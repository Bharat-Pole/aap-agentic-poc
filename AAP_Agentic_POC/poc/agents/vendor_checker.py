"""Vendor Checker Agent — deterministic supplier eligibility.

Looks up a SKU's primary vendor, its overall standing, the per-SKU approval
(catalog membership + MOQ + cost + lead time), and recommends a route:
AUTO-ISSUE only when the vendor is APPROVED at both levels, carries the SKU, and
the forecast quantity clears MOQ; otherwise DRAFT-FOR-APPROVAL with the precise
reason (suspended / pending / no-approved-vendor / below-MOQ).

It also provides :func:`consolidate_by_vendor`, which groups a batch of
auto-issue SKUs sharing one approved vendor into a single PO group (the S3
consolidation). The Approval Agent makes the *final* tier call; this agent only
recommends, because suppression (decided upstream) can still override.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import AutonomyTier, DraftReason
from data.database import get_session

if TYPE_CHECKING:
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.vendor_checker")

AGENT_NAME = "VendorCheckerAgent"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class VendorCheckerInput(BaseModel):
    """Input contract: the SKU and the forecast quantity to test against MOQ."""

    sku: str
    qty_needed: int | None = Field(
        None, description="Forecast order qty; None in advisory/suppress mode."
    )


class VendorAssessment(BaseModel):
    """Output contract: the supplier eligibility verdict for one SKU."""

    sku: str
    primary_vendor_id: str | None
    vendor_status: str | None = Field(None, description="vendor_master.status.")
    approval_status: str | None = Field(None, description="vendor_approval.approval_status.")
    in_catalog: bool = Field(..., description="Primary vendor carries the SKU.")
    moq: int | None
    unit_cost: float | None
    lead_time_days: int | None
    qty_meets_moq: bool
    approved_alternative_exists: bool
    recommended_route: AutonomyTier = Field(
        ..., description="AUTO_ISSUE or DRAFT_FOR_APPROVAL (never SUPPRESS here)."
    )
    draft_reason: DraftReason | None
    note: str


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def check_vendor(
    session: Session, sku: str, qty_needed: int | None
) -> VendorAssessment:
    """Assess primary-vendor eligibility and recommend a route for one SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to check.
        qty_needed: Forecast order quantity; ``None`` skips the MOQ gate
            (advisory/suppress paths, where no order is placed).

    Returns:
        A populated :class:`VendorAssessment`.

    Rationale: encodes the AUTO-ISSUE gate (approved at both levels + in-catalog
        + MOQ cleared) and, on failure, the single highest-priority draft reason.
    """
    appr = db.primary_approval(session, sku)
    inv = db.get_inventory(session, sku)
    primary_vendor_id = inv.primary_vendor_id if inv else None
    vendor = db.get_vendor(session, appr.vendor_id if appr else primary_vendor_id)

    in_catalog = appr is not None
    vendor_status = vendor.status if vendor else None
    approval_status = appr.approval_status if appr else None
    moq = appr.moq if appr else None
    unit_cost = appr.unit_cost if appr else None
    lead_time = appr.lead_time_days if appr else None

    # MOQ gate is skipped when there is no order quantity to test.
    meets_moq = True if qty_needed is None else (moq is not None and qty_needed >= moq)

    alt_vendor = appr.vendor_id if appr else primary_vendor_id
    alternatives = db.approved_alternatives(session, sku, exclude_vendor=alt_vendor)
    has_alt = len(alternatives) > 0

    reason = _draft_reason(appr, vendor, meets_moq, has_alt)
    if reason is None:
        route = AutonomyTier.AUTO_ISSUE
        note = (
            f"{appr.vendor_id} APPROVED, carries {sku}, "
            f"qty {qty_needed} ≥ MOQ {moq}: auto-issue."
        )
    else:
        route = AutonomyTier.DRAFT_FOR_APPROVAL
        note = f"draft ({reason.value}): {_reason_detail(reason, appr, vendor, qty_needed, moq, has_alt)}"

    return VendorAssessment(
        sku=sku,
        primary_vendor_id=appr.vendor_id if appr else primary_vendor_id,
        vendor_status=vendor_status,
        approval_status=approval_status,
        in_catalog=in_catalog,
        moq=moq,
        unit_cost=unit_cost,
        lead_time_days=lead_time,
        qty_meets_moq=meets_moq,
        approved_alternative_exists=has_alt,
        recommended_route=route,
        draft_reason=reason,
        note=note,
    )


def _draft_reason(appr, vendor, meets_moq: bool, has_alt: bool) -> DraftReason | None:
    """Return the highest-priority draft reason, or ``None`` for auto-issue.

    Args:
        appr: The primary :class:`VendorApproval` row, or ``None``.
        vendor: The parent :class:`VendorMaster` row, or ``None``.
        meets_moq: Whether the forecast quantity clears MOQ.
        has_alt: Whether an approved alternative vendor carries the SKU.

    Returns:
        A :class:`DraftReason`, or ``None`` if every AUTO-ISSUE condition holds.

    Rationale: suspension/pending vendor standing dominates a quantity shortfall —
        a blocked supplier can't be auto-issued regardless of MOQ.
    """
    # No primary approval row -> the primary vendor does not carry the SKU.
    if appr is None:
        return DraftReason.NO_APPROVED_VENDOR
    # Vendor-level gate first (a SUSPENDED parent escalates even if per-SKU APPROVED).
    if (vendor and vendor.status == "SUSPENDED") or appr.approval_status == "SUSPENDED":
        return DraftReason.SUSPENDED
    if (vendor and vendor.status == "PENDING") or appr.approval_status == "PENDING":
        return DraftReason.PENDING
    # Vendor is APPROVED at both levels; the only remaining blocker is quantity.
    if not meets_moq:
        return DraftReason.BELOW_MOQ
    return None


def _reason_detail(reason, appr, vendor, qty_needed, moq, has_alt: bool) -> str:
    """Build a one-line human explanation for a draft reason.

    Args:
        reason: The assigned :class:`DraftReason`.
        appr: Primary approval row (may be ``None``).
        vendor: Parent vendor row (may be ``None``).
        qty_needed: Forecast quantity.
        moq: Minimum order quantity.
        has_alt: Whether an approved alternative exists.

    Returns:
        A short rationale string for the audit log and the dashboard.

    Rationale: makes the draft path self-explanatory without re-deriving inputs.
    """
    vid = appr.vendor_id if appr else "primary vendor"
    if reason is DraftReason.NO_APPROVED_VENDOR:
        return f"no approved vendor carries this SKU (alternatives: {has_alt})."
    if reason is DraftReason.SUSPENDED:
        return f"{vid} is SUSPENDED; no approved backup ({has_alt})."
    if reason is DraftReason.PENDING:
        return f"{vid} is PENDING; no approved backup ({has_alt})."
    return f"qty {qty_needed} below MOQ {moq} for {vid}."


# ---------------------------------------------------------------------------
# Batch consolidation helper (S3)
# ---------------------------------------------------------------------------
def consolidate_by_vendor(
    assessments: list[VendorAssessment],
) -> dict[str, list[VendorAssessment]]:
    """Group auto-issue SKUs that share one approved vendor into PO groups.

    Args:
        assessments: A batch of vendor assessments (e.g. the S3 SKUs).

    Returns:
        A mapping ``vendor_id -> [assessments]`` containing only the
        AUTO-ISSUE-recommended SKUs, so each group becomes one consolidated PO.
        Draft/escalated SKUs are excluded (they go through human review).

    Rationale: PO consolidation cuts a separate order per SKU down to one order
        per vendor — the efficiency the S3 seasonal batch is meant to show.
    """
    groups: dict[str, list[VendorAssessment]] = defaultdict(list)
    for a in assessments:
        if a.recommended_route is AutonomyTier.AUTO_ISSUE and a.primary_vendor_id:
            groups[a.primary_vendor_id].append(a)
    return dict(groups)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Assess the vendor for ``state.sku`` and write it into the shared state.

    Args:
        state: Shared state; uses ``state.forecast.qty_needed`` for the MOQ gate.
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.vendor`` populated.

    Raises:
        ValueError: If the Demand Forecast Agent has not run yet.

    Rationale: the recommendation the Approval Agent combines with the stock
        signal to pick the final autonomy tier.
    """
    if state.forecast is None:
        raise ValueError("VendorCheckerAgent requires DemandForecast output first.")

    own = session is None
    s = session or get_session()
    try:
        assessment = check_vendor(s, state.sku, state.forecast.qty_needed)
        db.append_audit(
            s,
            run_id=state.run_id,
            agent=AGENT_NAME,
            event_type="VENDOR_CHECK",
            sku=state.sku,
            vendor_id=assessment.primary_vendor_id,
            summary=f"{state.sku}: {assessment.note}",
            details=assessment.model_dump(mode="json"),
        )
        s.commit()
        state.vendor = assessment
        log.info(
            "%s -> %s (%s)",
            state.sku,
            assessment.recommended_route.value,
            assessment.draft_reason.value if assessment.draft_reason else "ok",
        )
        return state
    finally:
        if own:
            s.close()
