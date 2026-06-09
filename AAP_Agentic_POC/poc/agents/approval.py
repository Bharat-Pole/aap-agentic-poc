"""Approval Agent — the deterministic autonomy-routing node.

This is the critical decision point. It combines the upstream signals into
exactly one autonomy tier:

* **SUPPRESS** — the Stock Monitor found inbound supply already covers the
  threshold. Suppression *overrides* everything else: no PO is ever placed.
* **AUTO-ISSUE** — a genuine breach whose vendor recommendation is auto-issue
  (approved at both levels, in-catalog, MOQ cleared).
* **DRAFT-FOR-APPROVAL** — a genuine breach the vendor recommendation could not
  auto-issue. The agent assembles the *structured* justification payload here;
  the natural-language justification text is generated later (LLM phase).

No LLM is involved — this is the routing logic the whole guardrail rests on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import AutonomyTier, DraftReason, StockSignal
from agents.stock_monitor import StockStatus
from agents.demand_forecast import ForecastResult
from agents.vendor_checker import VendorAssessment
from data.database import get_session

if TYPE_CHECKING:
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.approval")

AGENT_NAME = "ApprovalAgent"

#: Deterministic confidence per outcome — fixed so the routing is reproducible
#: and explainable (no probabilistic model behind the number).
_CONFIDENCE_SUPPRESS = 0.97
_CONFIDENCE_AUTO = 0.95
_CONFIDENCE_DRAFT: dict[DraftReason, float] = {
    DraftReason.SUSPENDED: 0.70,
    DraftReason.PENDING: 0.75,
    DraftReason.NO_APPROVED_VENDOR: 0.60,
    DraftReason.BELOW_MOQ: 0.80,
}

#: Audit event_type per tier (aligned with the data-model enum).
_EVENT_BY_TIER: dict[AutonomyTier, str] = {
    AutonomyTier.AUTO_ISSUE: "ROUTING_DECISION",
    AutonomyTier.DRAFT_FOR_APPROVAL: "DRAFT_CREATED",
    AutonomyTier.SUPPRESS: "SUPPRESSED",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ApprovalInput(BaseModel):
    """Input contract: the three upstream verdicts the router combines."""

    stock: StockStatus
    forecast: ForecastResult
    vendor: VendorAssessment


class ApprovalDecision(BaseModel):
    """Output contract: the final autonomy-tier routing decision.

    ``justification_payload`` holds the structured fields behind a draft (the
    raw material the LLM later turns into prose); it is ``None`` off the draft
    path. ``requires_human`` is the human-in-the-loop gate for Phase 2.
    """

    sku: str
    tier: AutonomyTier
    reason: str = Field(..., description="Why this tier, in one phrase.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    requires_human: bool
    justification_payload: dict[str, Any] | None = None
    note: str


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def classify(
    stock: StockStatus, forecast: ForecastResult, vendor: VendorAssessment
) -> ApprovalDecision:
    """Route a single SKU to exactly one autonomy tier.

    Args:
        stock: Stock Monitor verdict (carries the SUPPRESS override).
        forecast: Demand Forecast result (quantity / advisory cover).
        vendor: Vendor Checker recommendation and draft reason.

    Returns:
        A populated :class:`ApprovalDecision`.

    Raises:
        ValueError: If any required upstream field is missing.

    Rationale: suppression is checked first because covered stock must never
        generate a PO; only genuine breaches reach the vendor-based fork.
    """
    if stock is None or forecast is None or vendor is None:
        raise ValueError("ApprovalAgent requires stock, forecast, and vendor inputs.")

    sku = stock.sku

    # 1) Suppression overrides everything — inbound supply already covers it.
    if stock.signal is StockSignal.SUPPRESS:
        cover = forecast.weeks_of_cover
        reason = "effective stock already covers the threshold (in-transit supply)"
        return ApprovalDecision(
            sku=sku,
            tier=AutonomyTier.SUPPRESS,
            reason=reason,
            confidence=_CONFIDENCE_SUPPRESS,
            requires_human=False,
            justification_payload=None,
            note=(
                f"SUPPRESS: effective {stock.effective_stock} ≥ threshold "
                f"{stock.reorder_threshold}"
                + (f" (~{cover:.1f} wks cover)" if isinstance(cover, (int, float)) else "")
                + "; advisory + audit only."
            ),
        )

    # 2) Genuine breach -> follow the vendor recommendation.
    if vendor.recommended_route is AutonomyTier.AUTO_ISSUE:
        return ApprovalDecision(
            sku=sku,
            tier=AutonomyTier.AUTO_ISSUE,
            reason="approved vendor in-catalog and MOQ cleared",
            confidence=_CONFIDENCE_AUTO,
            requires_human=False,
            justification_payload=None,
            note=(
                f"AUTO-ISSUE: {vendor.primary_vendor_id} approved; "
                f"qty {forecast.qty_needed} ≥ MOQ {vendor.moq}."
            ),
        )

    # 3) Draft path -> assemble the structured justification payload.
    draft_reason = vendor.draft_reason or DraftReason.NO_APPROVED_VENDOR
    payload = _build_justification_payload(stock, forecast, vendor, draft_reason)
    return ApprovalDecision(
        sku=sku,
        tier=AutonomyTier.DRAFT_FOR_APPROVAL,
        reason=draft_reason.value,
        confidence=_CONFIDENCE_DRAFT.get(draft_reason, 0.7),
        requires_human=True,
        justification_payload=payload,
        note=f"DRAFT-FOR-APPROVAL ({draft_reason.value}): {vendor.note}",
    )


def _build_justification_payload(
    stock: StockStatus,
    forecast: ForecastResult,
    vendor: VendorAssessment,
    draft_reason: DraftReason,
) -> dict[str, Any]:
    """Collect the structured fields behind a draft decision.

    Args:
        stock: Stock Monitor verdict.
        forecast: Demand Forecast result.
        vendor: Vendor Checker recommendation.
        draft_reason: Why the breach could not be auto-issued.

    Returns:
        A JSON-serialisable dict of the deterministic inputs a human (and, later,
        the LLM) needs to justify the draft PO.

    Rationale: separating the *facts* from their eventual prose keeps the LLM's
        role confined to phrasing — every number here is computed deterministically.
    """
    return {
        "sku": stock.sku,
        "description": stock.description,
        "category": stock.category,
        "on_hand": stock.on_hand,
        "reorder_threshold": stock.reorder_threshold,
        "effective_stock": stock.effective_stock,
        "days_to_stockout": forecast.days_to_stockout,
        "weekly_avg": forecast.weekly_avg,
        "qty_needed": forecast.qty_needed,
        "promo_uplift_pct": forecast.promo_uplift_pct,
        "season_uplift_pct": forecast.season_uplift_pct,
        "combined_uplift": forecast.combined_uplift,
        "vendor_id": vendor.primary_vendor_id,
        "vendor_status": vendor.vendor_status,
        "approval_status": vendor.approval_status,
        "moq": vendor.moq,
        "unit_cost": vendor.unit_cost,
        "approved_alternative_exists": vendor.approved_alternative_exists,
        "draft_reason": draft_reason.value,
    }


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Route ``state.sku`` to an autonomy tier and write it into the state.

    Args:
        state: Shared state carrying stock, forecast, and vendor verdicts.
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.decision`` populated.

    Raises:
        ValueError: If any required upstream verdict is missing.

    Rationale: the routing decision is the audit log's centrepiece — recorded
        with its full deterministic input set under the tier-specific event type.
    """
    decision = classify(state.stock, state.forecast, state.vendor)

    own = session is None
    s = session or get_session()
    try:
        db.append_audit(
            s,
            run_id=state.run_id,
            agent=AGENT_NAME,
            event_type=_EVENT_BY_TIER[decision.tier],
            sku=decision.sku,
            vendor_id=state.vendor.primary_vendor_id,
            autonomy_tier=decision.tier.value,
            summary=f"{decision.sku}: {decision.tier.display} — {decision.reason}",
            details={
                "tier": decision.tier.value,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "requires_human": decision.requires_human,
                "justification_payload": decision.justification_payload,
            },
        )
        s.commit()
        state.decision = decision
        log.info(
            "%s -> %s (confidence=%.2f, human=%s)",
            decision.sku,
            decision.tier.display,
            decision.confidence,
            decision.requires_human,
        )
        return state
    finally:
        if own:
            s.close()
