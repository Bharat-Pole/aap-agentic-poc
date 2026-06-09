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

import config
from agents import db
from agents.enums import AutonomyTier, DraftReason, StockSignal
from agents.stock_monitor import StockStatus
from agents.demand_forecast import ForecastResult
from agents.vendor_checker import VendorAssessment
from data.database import get_session
from llm import provider

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
    justification_narrative: str | None = Field(
        None,
        description="Plain-language 3-4 sentence rationale for the DP (draft path only).",
    )
    narrative_source: str | None = Field(
        None, description="Which path wrote the narrative: gemini / ollama / template."
    )
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

    # 3) Draft path -> assemble the structured justification payload and a
    #    deterministic narrative. run() upgrades the narrative via the LLM when
    #    enabled; classify() stays pure so direct callers still get usable prose.
    draft_reason = vendor.draft_reason or DraftReason.NO_APPROVED_VENDOR
    payload = _build_justification_payload(stock, forecast, vendor, draft_reason)
    return ApprovalDecision(
        sku=sku,
        tier=AutonomyTier.DRAFT_FOR_APPROVAL,
        reason=draft_reason.value,
        confidence=_CONFIDENCE_DRAFT.get(draft_reason, 0.7),
        requires_human=True,
        justification_payload=payload,
        justification_narrative=_fallback_narrative(payload),
        narrative_source=provider.TEMPLATE,
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
# Justification narrative (the LLM edge — draft path only)
# ---------------------------------------------------------------------------
#: System instruction pinning the model to the supplied facts. The hard rule —
#: "use ONLY the numbers given" — is what keeps an LLM out of the decision: it
#: phrases the deterministic facts, it does not compute or invent them.
_NARRATIVE_SYSTEM = (
    "You are a supply-chain assistant writing for a Demand Planner at an "
    "automotive-parts distributor. Write a concise 3-4 sentence justification for "
    "a draft purchase order that needs human approval. Cover: why the PO is "
    "needed, how urgent it is, and the vendor caveat that blocked auto-issue. "
    "Use ONLY the figures provided below — do NOT invent SKUs, quantities, dates, "
    "costs, or vendor names. Plain professional prose, no bullet points, no "
    "headings, no preamble."
)


def _narrative_prompt(payload: dict[str, Any]) -> str:
    """Render the structured justification fields into an LLM prompt.

    Args:
        payload: The deterministic fields from :func:`_build_justification_payload`.

    Returns:
        A labelled facts block the model is told to phrase (and not exceed).

    Rationale: passing explicit ``label: value`` lines (rather than free text)
        makes it easy for the model to stay on the given numbers and easy for a
        reviewer to confirm nothing was invented.
    """
    days = payload.get("days_to_stockout")
    days_txt = f"{days} days" if isinstance(days, (int, float)) else "unknown"
    return (
        "Facts (use only these):\n"
        f"- SKU: {payload.get('sku')} ({payload.get('description')}, "
        f"{payload.get('category')})\n"
        f"- On hand: {payload.get('on_hand')} units; reorder threshold: "
        f"{payload.get('reorder_threshold')}; effective stock: "
        f"{payload.get('effective_stock')}\n"
        f"- Estimated days to stockout: {days_txt}; weekly average demand: "
        f"{payload.get('weekly_avg')}\n"
        f"- Recommended order quantity: {payload.get('qty_needed')} units\n"
        f"- Demand uplift applied: promo {payload.get('promo_uplift_pct')}%, "
        f"season {payload.get('season_uplift_pct')}% "
        f"(combined {payload.get('combined_uplift')}x)\n"
        f"- Vendor: {payload.get('vendor_id')} — status "
        f"{payload.get('vendor_status')}, approval {payload.get('approval_status')}, "
        f"MOQ {payload.get('moq')}, unit cost {payload.get('unit_cost')}\n"
        f"- Reason auto-issue was blocked: {payload.get('draft_reason')}\n"
        f"- Approved alternative vendor available: "
        f"{payload.get('approved_alternative_exists')}\n\n"
        "Write the 3-4 sentence justification now."
    )


def _fallback_narrative(payload: dict[str, Any]) -> str:
    """Build a deterministic 3-sentence narrative from the structured fields.

    Args:
        payload: The deterministic justification fields.

    Returns:
        A fixed-form rationale used when the LLM is disabled or unreachable.

    Rationale: the template last resort — same facts, no model — so the draft
        path always carries readable prose even fully offline.
    """
    days = payload.get("days_to_stockout")
    days_txt = (
        f"about {days} days" if isinstance(days, (int, float)) else "an unknown horizon"
    )
    return (
        f"{payload.get('sku')} ({payload.get('description')}) has fallen to "
        f"{payload.get('on_hand')} units against a reorder threshold of "
        f"{payload.get('reorder_threshold')}, with {days_txt} of cover remaining. "
        f"A replenishment of {payload.get('qty_needed')} units is recommended from "
        f"{payload.get('vendor_id')}, but the order cannot be auto-issued because "
        f"of '{payload.get('draft_reason')}'. Please review and approve before any "
        f"PO is sent to the vendor."
    )


def _attach_narrative(decision: ApprovalDecision) -> None:
    """Upgrade a draft decision's narrative via the LLM, in place.

    Args:
        decision: The draft :class:`ApprovalDecision` (mutated on success).

    Rationale: the single LLM touch-point on the approval side — it only ever
        rephrases the already-computed facts, so a provider outage or a disabled
        LLM simply leaves the deterministic template narrative in place.
    """
    payload = decision.justification_payload or {}
    result = provider.generate_with_provider(
        _narrative_prompt(payload),
        system=_NARRATIVE_SYSTEM,
        fallback=decision.justification_narrative,
    )
    decision.justification_narrative = result.text
    decision.narrative_source = result.provider


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

    # LLM edge (a): phrase the draft justification for the DP. Routing above is
    # already final and deterministic — this only writes prose, never decides.
    if decision.tier is AutonomyTier.DRAFT_FOR_APPROVAL and config.USE_LLM:
        _attach_narrative(decision)

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
                "justification_narrative": decision.justification_narrative,
                "narrative_source": decision.narrative_source,
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
