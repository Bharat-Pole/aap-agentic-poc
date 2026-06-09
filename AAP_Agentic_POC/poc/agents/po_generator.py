"""PO Generator — builds purchase orders and enforces the write guardrail.

A PO line reaches Blue Yonder (via :func:`agents.mock_blue_yonder.write_po`)
**only** when:

* the autonomy tier is ``AUTO_ISSUE`` (pre-approved vendor + SKU), or
* the tier is ``DRAFT_FOR_APPROVAL`` **and** a human has approved it
  (``human_approved=True``).

It is **never** written on ``SUPPRESS``. This module is where that guardrail is
enforced, in one place, before any write occurs. It supports single-SKU POs and
vendor-consolidated multi-SKU POs (the S3 batch). On the auto-issue path the PO
status is ``ISSUED``; on an approved draft it is ``APPROVED``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import AutonomyTier
from agents.mock_blue_yonder import POLineSpec, write_po
from data.database import get_session

if TYPE_CHECKING:
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.po_generator")

AGENT_NAME = "POGenerator"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class POLine(BaseModel):
    """One written PO line, echoed back for the state and the dashboard."""

    sku: str
    quantity: int
    unit_cost: float
    total_cost: float
    line_no: int


class POGeneratorInput(BaseModel):
    """Input contract: the SKU and the (possibly human-set) approval flags."""

    sku: str
    human_approved: bool | None = None
    approved_by: str | None = None


class POResult(BaseModel):
    """Output contract: the outcome of a PO-generation attempt."""

    po_number: str | None
    vendor_id: str | None
    autonomy_tier: AutonomyTier | None
    status: str | None = Field(None, description="ISSUED or APPROVED when written.")
    lines: list[POLine] = Field(default_factory=list)
    total_cost: float = 0.0
    written: bool = Field(..., description="True iff lines were written to Blue Yonder.")
    skipped_reason: str | None = Field(
        None, description="Why no PO was written (e.g. suppressed / awaiting approval)."
    )


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------
def _write_allowed(
    tier: AutonomyTier, human_approved: bool | None
) -> tuple[bool, str | None]:
    """Decide whether a PO may be written, and the status it carries.

    Args:
        tier: The autonomy tier from the Approval Agent.
        human_approved: Whether a human approved a draft (None/False = not yet).

    Returns:
        ``(allowed, skipped_reason)`` — ``skipped_reason`` is ``None`` when allowed.

    Rationale: the single guardrail — auto-issue writes freely; a draft writes
        only after explicit human approval; suppression never writes.
    """
    if tier is AutonomyTier.AUTO_ISSUE:
        return True, None
    if tier is AutonomyTier.DRAFT_FOR_APPROVAL:
        if human_approved:
            return True, None
        return False, "draft awaiting human approval — no write"
    return False, "suppressed — guardrail forbids writing a PO"


def _po_status(tier: AutonomyTier) -> str:
    """Map the autonomy tier to the written PO status.

    Args:
        tier: The autonomy tier being written.

    Returns:
        ``ISSUED`` for auto-issue, ``APPROVED`` for an approved draft.

    Rationale: the status records *how* the PO became legitimate (machine vs human).
    """
    return "ISSUED" if tier is AutonomyTier.AUTO_ISSUE else "APPROVED"


# ---------------------------------------------------------------------------
# Single-SKU generation
# ---------------------------------------------------------------------------
def generate_for_state(
    state: "ReplenishmentState", session: Session
) -> POResult:
    """Build (and, if allowed, write) the PO for one SKU from the shared state.

    Args:
        state: Shared state carrying the decision, forecast, and vendor verdicts.
        session: Open ORM session (caller commits).

    Returns:
        A :class:`POResult` — written, or skipped with a reason.

    Raises:
        ValueError: If the Approval decision is missing.

    Rationale: the guardrail is checked before any line is built, so a suppressed
        or unapproved-draft SKU can never reach :func:`write_po`.
    """
    if state.decision is None:
        raise ValueError("POGenerator requires an Approval decision first.")

    tier = state.decision.tier
    allowed, skipped = _write_allowed(tier, state.human_approved)
    if not allowed:
        log.info("%s -> no PO (%s)", state.sku, skipped)
        result = POResult(
            po_number=None,
            vendor_id=state.vendor.primary_vendor_id if state.vendor else None,
            autonomy_tier=tier,
            written=False,
            skipped_reason=skipped,
        )
        db.append_audit(
            session,
            run_id=state.run_id,
            agent=AGENT_NAME,
            event_type="PO_SKIPPED",
            sku=state.sku,
            vendor_id=result.vendor_id,
            autonomy_tier=tier.value,
            summary=f"{state.sku}: no PO — {skipped}",
        )
        return result

    qty = state.forecast.qty_needed if state.forecast else None
    if qty is None or qty <= 0:
        raise ValueError(f"Cannot write a PO for {state.sku}: no positive qty_needed.")

    vendor = state.vendor
    po_number = db.next_po_number(session)
    status = _po_status(tier)
    justification = _draft_justification_text(state) if tier is AutonomyTier.DRAFT_FOR_APPROVAL else None

    rows = write_po(
        session,
        po_number=po_number,
        vendor_id=vendor.primary_vendor_id,
        lines=[POLineSpec(sku=state.sku, quantity=qty, unit_cost=vendor.unit_cost or 0.0)],
        autonomy_tier=tier.value,
        status=status,
        justification=justification,
        approved_by=state.approved_by if tier is AutonomyTier.DRAFT_FOR_APPROVAL else None,
    )
    result = _result_from_rows(po_number, vendor.primary_vendor_id, tier, status, rows)
    _audit_written(session, state.run_id, result, state.sku)
    log.info("%s -> PO %s (%s, qty=%d)", state.sku, po_number, status, qty)
    return result


# ---------------------------------------------------------------------------
# Consolidated multi-SKU generation (S3)
# ---------------------------------------------------------------------------
def generate_consolidated(
    session: Session,
    *,
    run_id: str | None,
    vendor_id: str,
    lines: list[POLineSpec],
    tier: AutonomyTier = AutonomyTier.AUTO_ISSUE,
) -> POResult:
    """Write one consolidated PO covering several SKUs from a shared vendor.

    Args:
        session: Open ORM session (caller commits).
        run_id: Correlation id for the audit row.
        vendor_id: The shared approved vendor.
        lines: One line spec per SKU in the consolidation group.
        tier: Autonomy tier (auto-issue for the S3 batch).

    Returns:
        A :class:`POResult` with one PO number spanning all lines.

    Raises:
        ValueError: If ``lines`` is empty or the tier is not writable.

    Rationale: collapses N per-SKU orders into one order per vendor — the S3
        consolidation that demonstrates the efficiency win.
    """
    if not lines:
        raise ValueError("generate_consolidated requires at least one line.")
    allowed, skipped = _write_allowed(tier, human_approved=True if tier is AutonomyTier.DRAFT_FOR_APPROVAL else None)
    if not allowed:
        raise ValueError(f"Consolidated write not allowed for tier {tier.value}: {skipped}")

    po_number = db.next_po_number(session)
    status = _po_status(tier)
    rows = write_po(
        session,
        po_number=po_number,
        vendor_id=vendor_id,
        lines=lines,
        autonomy_tier=tier.value,
        status=status,
    )
    result = _result_from_rows(po_number, vendor_id, tier, status, rows)
    db.append_audit(
        session,
        run_id=run_id,
        agent=AGENT_NAME,
        event_type="PO_WRITTEN",
        vendor_id=vendor_id,
        autonomy_tier=tier.value,
        po_number=po_number,
        summary=(
            f"Consolidated PO {po_number} for {vendor_id}: "
            f"{len(rows)} lines, total {result.total_cost:.2f}."
        ),
        details=result.model_dump(mode="json"),
    )
    log.info("Consolidated PO %s vendor=%s lines=%d", po_number, vendor_id, len(rows))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _result_from_rows(po_number, vendor_id, tier, status, rows) -> POResult:
    """Assemble a :class:`POResult` from written ORM rows.

    Args:
        po_number: The PO number written.
        vendor_id: The supplier.
        tier: Autonomy tier.
        status: Written status (ISSUED/APPROVED).
        rows: The :class:`BlueYonderPO` rows produced by ``write_po``.

    Returns:
        A populated, written :class:`POResult`.

    Rationale: keeps the row->result mapping in one spot for both write paths.
    """
    lines = [
        POLine(
            sku=r.sku,
            quantity=r.quantity,
            unit_cost=r.unit_cost,
            total_cost=r.total_cost,
            line_no=r.line_no,
        )
        for r in rows
    ]
    return POResult(
        po_number=po_number,
        vendor_id=vendor_id,
        autonomy_tier=tier,
        status=status,
        lines=lines,
        total_cost=round(sum(l.total_cost for l in lines), 2),
        written=True,
        skipped_reason=None,
    )


def _audit_written(session, run_id, result: POResult, sku: str) -> None:
    """Append a PO_WRITTEN audit row for a single-SKU PO.

    Args:
        session: Open ORM session.
        run_id: Correlation id.
        result: The written PO result.
        sku: The SKU ordered.

    Rationale: every executed PO is evidence and must appear in the audit trail.
    """
    db.append_audit(
        session,
        run_id=run_id,
        agent=AGENT_NAME,
        event_type="PO_WRITTEN",
        sku=sku,
        vendor_id=result.vendor_id,
        autonomy_tier=result.autonomy_tier.value if result.autonomy_tier else None,
        po_number=result.po_number,
        summary=(
            f"{sku}: PO {result.po_number} {result.status}, "
            f"total {result.total_cost:.2f}."
        ),
        details=result.model_dump(mode="json"),
    )


def _draft_justification_text(state: "ReplenishmentState") -> str:
    """Produce a plain templated justification for an approved draft PO.

    Args:
        state: Shared state with the draft justification payload.

    Returns:
        A deterministic one-paragraph rationale.

    Rationale: Phase 1 uses a fixed template; the LLM replaces this on the draft
        path in a later phase (the only natural-language touch-point besides notifications).
    """
    p = (state.decision.justification_payload or {}) if state.decision else {}
    return (
        f"Draft PO for {p.get('sku')} ({p.get('description')}): on-hand "
        f"{p.get('on_hand')} below threshold {p.get('reorder_threshold')}; "
        f"~{p.get('days_to_stockout')} days to stockout. Recommended qty "
        f"{p.get('qty_needed')} from {p.get('vendor_id')} "
        f"(reason: {p.get('draft_reason')}). Approved by {state.approved_by}."
    )


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Generate the PO for ``state.sku`` (single-SKU path) into the state.

    Args:
        state: Shared state carrying the routing decision.
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.po`` populated (written or skipped).

    Rationale: the guardrail node — the only pipeline step that can place an order.
    """
    own = session is None
    s = session or get_session()
    try:
        result = generate_for_state(state, s)
        s.commit()
        state.po = result
        return state
    finally:
        if own:
            s.close()
