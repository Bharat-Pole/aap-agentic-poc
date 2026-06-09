"""Demand Forecast Agent — deterministic demand sizing.

For a breached SKU it computes how much to order: a baseline weekly demand from
the 90-day offtake history, inflated by any active promo and seasonal uplift and
by a planning-horizon multiplier, then padded with a safety buffer and rounded
up to the pack size. For a SUPPRESS SKU it runs in *advisory* mode — it reports
weeks-of-cover but deliberately sets no order quantity (we are not ordering).

All arithmetic is deterministic. A statsmodels forecaster can later replace the
weekly-average baseline without changing this contract; the mean is sufficient
and reproducible for the POC.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import StockSignal
from config import (
    DEFAULT_LEAD_MULTIPLIER,
    PEAK_LEAD_MULTIPLIER,
    SAFETY_BUFFER_FACTOR,
)
from data.database import get_session

if TYPE_CHECKING:
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.demand_forecast")

AGENT_NAME = "DemandForecastAgent"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class DemandForecastInput(BaseModel):
    """Input contract: the SKU and whether to run advisory-only (suppress)."""

    sku: str
    advisory_only: bool = Field(
        False, description="True for SUPPRESS SKUs: report cover, set no qty."
    )


class ForecastResult(BaseModel):
    """Output contract: the demand sizing for one SKU.

    ``qty_needed`` is the order quantity (pack-rounded) on a replenishment path,
    or ``None`` in advisory mode, where ``weeks_of_cover`` is reported instead.
    """

    sku: str
    weekly_avg: float
    daily_avg: float
    promo_uplift_pct: float
    season_uplift_pct: float
    combined_uplift: float
    lead_multiplier: float
    safety_buffer: int
    qty_needed: int | None
    days_to_stockout: float
    weeks_of_cover: float | None
    advisory: bool
    forecast_basis: str = Field(..., description="Human-readable derivation.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _planning_multiplier(peak_season: bool, db_multiplier: float) -> float:
    """Pick the planning-horizon multiplier for a SKU.

    Args:
        peak_season: Whether the SKU is in a peak window.
        db_multiplier: The SKU's ``inventory.lead_multiplier`` value.

    Returns:
        The seeded value when it is an explicit non-neutral override (> 1.0),
        else the policy default (2.0 peak / 1.5 normal).

    Rationale: honours an explicit per-SKU override (the seed sets 2.0 for the S3
        peak SKUs) while applying the Phase-1 policy default to everything else.
    """
    if db_multiplier and db_multiplier > 1.0:
        return float(db_multiplier)
    return PEAK_LEAD_MULTIPLIER if peak_season else DEFAULT_LEAD_MULTIPLIER


def _ceil_to_pack(qty: float, pack_size: int) -> int:
    """Round an order quantity up to a whole number of packs.

    Args:
        qty: Raw computed quantity.
        pack_size: Units per pack (rounding unit); treated as 1 if non-positive.

    Returns:
        The smallest multiple of ``pack_size`` that is ≥ ``qty`` (and ≥ 0).

    Rationale: you cannot order a fractional pack; always round up to cover demand.
    """
    pack = pack_size if pack_size and pack_size > 0 else 1
    if qty <= 0:
        return 0
    return int(math.ceil(qty / pack) * pack)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def forecast_sku(
    session: Session, sku: str, advisory_only: bool = False
) -> ForecastResult:
    """Compute the demand sizing (or advisory cover) for one SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to size.
        advisory_only: When True (SUPPRESS path), report weeks-of-cover and set
            ``qty_needed`` to ``None`` — no order is being placed.

    Returns:
        A populated :class:`ForecastResult`.

    Raises:
        ValueError: If the SKU is unknown.

    Rationale: one deterministic place that turns history + uplifts into a number,
        so the same inputs always yield the same order quantity.
    """
    inv = db.get_inventory(session, sku)
    if inv is None:
        raise ValueError(f"Unknown SKU: {sku!r}")

    units = db.offtake_units(session, sku)
    daily_avg = (sum(units) / len(units)) if units else 0.0
    weekly_avg = daily_avg * 7.0

    promo = db.active_promo_uplift(session, sku)
    season = db.season_uplift(session, inv.category)
    combined_uplift = (1.0 + promo) * (1.0 + season)
    multiplier = _planning_multiplier(inv.peak_season, inv.lead_multiplier)
    safety_buffer = int(round(inv.safety_stock * SAFETY_BUFFER_FACTOR))

    # days_to_stockout uses raw daily demand (uplifts describe *future* pressure
    # on the order size, not the burn-down of stock on hand today).
    days_to_stockout = (inv.on_hand / daily_avg) if daily_avg > 0 else float("inf")

    # weeks_of_cover (advisory mode) is reported against *effective* stock;
    # recompute it here so this agent stays self-contained.
    in_transit = sum(po.quantity for po in db.intransit_open_pos(session, sku))
    effective = inv.on_hand + in_transit

    if advisory_only:
        weeks_of_cover = (effective / weekly_avg) if weekly_avg > 0 else float("inf")
        qty_needed: int | None = None
        basis = (
            f"ADVISORY: effective {effective} / weekly_avg {weekly_avg:.0f} "
            f"= {weeks_of_cover:.1f} weeks of cover; no order placed."
        )
    else:
        raw = weekly_avg * combined_uplift * multiplier + safety_buffer
        qty_needed = _ceil_to_pack(raw, inv.pack_size)
        weeks_of_cover = None
        basis = (
            f"weekly_avg {weekly_avg:.0f} ×(1+promo {promo:.2f})×(1+season {season:.2f}) "
            f"×lead {multiplier:g} + buffer {safety_buffer} = {raw:.0f} "
            f"→ {qty_needed} (pack {inv.pack_size})"
        )

    return ForecastResult(
        sku=sku,
        weekly_avg=round(weekly_avg, 2),
        daily_avg=round(daily_avg, 4),
        promo_uplift_pct=promo,
        season_uplift_pct=season,
        combined_uplift=round(combined_uplift, 4),
        lead_multiplier=multiplier,
        safety_buffer=safety_buffer,
        qty_needed=qty_needed,
        days_to_stockout=round(days_to_stockout, 2) if math.isfinite(days_to_stockout) else days_to_stockout,
        weeks_of_cover=(round(weeks_of_cover, 2) if weeks_of_cover is not None and math.isfinite(weeks_of_cover) else weeks_of_cover),
        advisory=advisory_only,
        forecast_basis=basis,
    )


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Forecast demand for ``state.sku`` and write it into the shared state.

    Args:
        state: Shared pipeline state; must already carry ``state.stock``.
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.forecast`` populated.

    Raises:
        ValueError: If the Stock Monitor has not run yet.

    Rationale: advisory mode is selected from the upstream SUPPRESS signal so a
        false alarm never produces an order quantity.
    """
    if state.stock is None:
        raise ValueError("DemandForecastAgent requires StockMonitor output first.")

    advisory = state.stock.signal is StockSignal.SUPPRESS
    own = session is None
    s = session or get_session()
    try:
        result = forecast_sku(s, state.sku, advisory_only=advisory)
        db.append_audit(
            s,
            run_id=state.run_id,
            agent=AGENT_NAME,
            event_type="FORECAST",
            sku=state.sku,
            summary=f"{state.sku}: {result.forecast_basis}",
            details=result.model_dump(mode="json"),
        )
        s.commit()
        state.forecast = result
        log.info(
            "%s -> qty_needed=%s advisory=%s", state.sku, result.qty_needed, advisory
        )
        return state
    finally:
        if own:
            s.close()
