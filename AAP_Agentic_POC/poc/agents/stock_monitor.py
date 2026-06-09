"""Stock Monitor Agent — deterministic breach detection.

Reads the warehouse position for a SKU and classifies it against the reorder
policy. The one subtlety is *effective stock*: on-hand plus inbound (in-transit)
open-PO quantity. A SKU under its threshold on-hand but already covered by
inbound supply is a false alarm and is flagged ``SUPPRESS`` rather than ordered
again (the S4 case). Everything here is pure arithmetic — no LLM, no forecasting.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import StockSignal
from config import CRITICAL_LOW_RATIO
from data.database import get_session

if TYPE_CHECKING:  # avoid an import cycle; state imports this module's models.
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.stock_monitor")

AGENT_NAME = "StockMonitorAgent"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class StockMonitorInput(BaseModel):
    """Input contract: the single SKU to evaluate."""

    sku: str = Field(..., description="SKU id to evaluate, e.g. SKU-123.")


class InTransitRef(BaseModel):
    """One inbound open PO contributing to effective stock."""

    po_number: str
    quantity: int
    eta_days: int
    status: str


class StockStatus(BaseModel):
    """Output contract: the Stock Monitor's per-SKU verdict.

    Carries the arithmetic behind the call so downstream agents and the audit
    log never have to re-query: effective stock, the inbound refs, and the
    classified :class:`StockSignal`.
    """

    sku: str
    description: str
    category: str
    on_hand: int
    reorder_threshold: int
    in_transit_qty: int
    effective_stock: int
    signal: StockSignal
    is_breach: bool = Field(..., description="True for any non-HEALTHY signal.")
    in_transit_refs: list[InTransitRef] = Field(default_factory=list)
    note: str = Field(..., description="One-line human-readable rationale.")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def evaluate_sku(session: Session, sku: str) -> StockStatus:
    """Classify one SKU's stock position against the reorder policy.

    Args:
        session: Open ORM session.
        sku: SKU id to evaluate.

    Returns:
        A :class:`StockStatus` with effective stock and the classified signal.

    Raises:
        ValueError: If the SKU is unknown.

    Rationale: the single source of the breach/suppress/healthy decision —
        ``on_hand`` triggers the breach, but *effective* stock decides suppression.
    """
    inv = db.get_inventory(session, sku)
    if inv is None:
        raise ValueError(f"Unknown SKU: {sku!r}")

    inbound = db.intransit_open_pos(session, sku)
    in_transit_qty = sum(po.quantity for po in inbound)
    effective = inv.on_hand + in_transit_qty
    refs = [
        InTransitRef(
            po_number=po.po_number,
            quantity=po.quantity,
            eta_days=po.eta_days,
            status=po.status,
        )
        for po in inbound
    ]

    threshold = inv.reorder_threshold
    if inv.on_hand >= threshold:
        signal = StockSignal.HEALTHY
        note = f"on_hand {inv.on_hand} ≥ threshold {threshold}: healthy, no action."
    elif effective >= threshold:
        # On-hand breaches, but inbound supply already covers it -> false alarm.
        signal = StockSignal.SUPPRESS
        note = (
            f"on_hand {inv.on_hand} < {threshold} but effective {effective} "
            f"(+{in_transit_qty} in-transit) ≥ {threshold}: suppress."
        )
    elif inv.on_hand < CRITICAL_LOW_RATIO * threshold:
        signal = StockSignal.CRITICAL_LOW
        note = (
            f"on_hand {inv.on_hand} < {CRITICAL_LOW_RATIO:g}×{threshold} "
            f"= {CRITICAL_LOW_RATIO * threshold:g}: critically low."
        )
    else:
        signal = StockSignal.LOW_STOCK
        note = f"effective {effective} < threshold {threshold}: low stock."

    return StockStatus(
        sku=inv.sku,
        description=inv.description,
        category=inv.category,
        on_hand=inv.on_hand,
        reorder_threshold=threshold,
        in_transit_qty=in_transit_qty,
        effective_stock=effective,
        signal=signal,
        is_breach=signal is not StockSignal.HEALTHY,
        in_transit_refs=refs,
        note=note,
    )


def scan_all(session: Session, run_id: str | None = None) -> list[StockStatus]:
    """Scan the whole warehouse and return only the flagged (non-healthy) SKUs.

    Args:
        session: Open ORM session.
        run_id: Optional correlation id for the audit rows.

    Returns:
        :class:`StockStatus` for every SKU that breached or was suppressed,
        in SKU order. Healthy SKUs are skipped.

    Rationale: this is what seeds the per-SKU pipeline runs (one run per breach);
        it also proves the monitor ignores the ~40 healthy background SKUs.
    """
    flagged: list[StockStatus] = []
    for inv in db.all_inventory(session):
        status = evaluate_sku(session, inv.sku)
        if status.signal is StockSignal.HEALTHY:
            continue
        _audit(session, run_id, status)
        flagged.append(status)
    log.info("scan_all flagged %d SKU(s)", len(flagged))
    return flagged


def _audit(session: Session, run_id: str | None, status: StockStatus) -> None:
    """Append the Stock Monitor's decision to the audit log.

    Args:
        session: Open ORM session.
        run_id: Correlation id.
        status: The verdict to record.

    Rationale: every detection — including a suppression candidate — is evidence.
    """
    event = "SUPPRESS_CANDIDATE" if status.signal is StockSignal.SUPPRESS else "STOCK_BREACH"
    db.append_audit(
        session,
        run_id=run_id,
        agent=AGENT_NAME,
        event_type=event,
        sku=status.sku,
        summary=f"{status.sku}: {status.signal.value} — {status.note}",
        details=status.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Evaluate ``state.sku`` and write the verdict into the shared state.

    Args:
        state: The shared pipeline state (its ``sku`` field selects the target).
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.stock`` populated.

    Rationale: the pipeline's first node — turns a SKU into a classified breach.
    """
    own = session is None
    s = session or get_session()
    try:
        status = evaluate_sku(s, state.sku)
        _audit(s, state.run_id, status)
        s.commit()
        state.stock = status
        log.info("%s -> %s", state.sku, status.signal.value)
        return state
    finally:
        if own:
            s.close()
