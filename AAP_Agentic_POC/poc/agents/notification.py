"""Notification Agent — composes and records the Demand Planner message.

Resolves the DP who owns the SKU's sub-category, then builds a notification
whose channel and urgency are derived deterministically from the autonomy tier:

* AUTO-ISSUE  -> Slack, normal urgency ("PO placed, FYI").
* DRAFT       -> Email, high urgency ("action required: approve/reject").
* SUPPRESS    -> Slack, low urgency ("no action: covered by inbound supply").

Phase 1 uses plain templated strings; the LLM later phrases the ``body`` only.
The routing facts in ``subject`` and the references stay deterministic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import db
from agents.enums import AutonomyTier, Channel, Urgency
from data.database import get_session
from data.models import Notification

if TYPE_CHECKING:
    from agents.state import ReplenishmentState

log = logging.getLogger("agents.notification")

AGENT_NAME = "NotificationAgent"

#: Channel + urgency per tier (deterministic; no LLM).
_CHANNEL_BY_TIER: dict[AutonomyTier, Channel] = {
    AutonomyTier.AUTO_ISSUE: Channel.SLACK,
    AutonomyTier.DRAFT_FOR_APPROVAL: Channel.EMAIL,
    AutonomyTier.SUPPRESS: Channel.SLACK,
}
_URGENCY_BY_TIER: dict[AutonomyTier, Urgency] = {
    AutonomyTier.AUTO_ISSUE: Urgency.NORMAL,
    AutonomyTier.DRAFT_FOR_APPROVAL: Urgency.HIGH,
    AutonomyTier.SUPPRESS: Urgency.LOW,
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class NotificationInput(BaseModel):
    """Input contract: the SKU whose decision should be communicated."""

    sku: str


class NotificationResult(BaseModel):
    """Output contract: the notification record composed for the DP."""

    recipient: str
    channel: Channel
    urgency: Urgency
    subject: str
    body: str
    sku: str | None
    po_number: str | None
    autonomy_tier: AutonomyTier | None
    status: str = "SENT"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def compose(state: "ReplenishmentState", recipient_name: str) -> NotificationResult:
    """Build the notification record for a routed SKU.

    Args:
        state: Shared state carrying the decision and (optional) PO result.
        recipient_name: Display name of the resolved Demand Planner.

    Returns:
        A populated :class:`NotificationResult` (templated ``body``).

    Raises:
        ValueError: If no routing decision is present.

    Rationale: channel/urgency follow the tier so the DP's attention is matched
        to whether action is required — without any LLM involvement yet.
    """
    if state.decision is None:
        raise ValueError("NotificationAgent requires an Approval decision first.")

    tier = state.decision.tier
    channel = _CHANNEL_BY_TIER[tier]
    urgency = _URGENCY_BY_TIER[tier]
    sku = state.sku
    po_number = state.po.po_number if state.po else None

    subject, body = _template(state, tier, recipient_name, po_number)
    return NotificationResult(
        recipient=recipient_name,
        channel=channel,
        urgency=urgency,
        subject=subject,
        body=body,
        sku=sku,
        po_number=po_number,
        autonomy_tier=tier,
        status="SENT",
    )


def _template(
    state: "ReplenishmentState",
    tier: AutonomyTier,
    recipient_name: str,
    po_number: str | None,
) -> tuple[str, str]:
    """Render the templated subject and body for a tier.

    Args:
        state: Shared state (decision, forecast, vendor, po).
        tier: The autonomy tier.
        recipient_name: DP display name.
        po_number: The PO number, if one was written.

    Returns:
        ``(subject, body)`` plain strings.

    Rationale: deterministic phrasing for Phase 1; the LLM later replaces only
        the body, leaving these facts intact as the ground truth.
    """
    sku = state.sku
    desc = state.stock.description if state.stock else sku
    qty = state.forecast.qty_needed if state.forecast else None
    vendor = state.vendor.primary_vendor_id if state.vendor else None

    if tier is AutonomyTier.AUTO_ISSUE:
        subject = f"[Auto-issued] PO {po_number} placed for {sku}"
        body = (
            f"Hi {recipient_name}, a purchase order was auto-issued for {sku} "
            f"({desc}): {qty} units to {vendor} (PO {po_number}). On-hand had "
            f"fallen to {state.stock.on_hand}/{state.stock.reorder_threshold}. "
            f"No action needed — sharing for visibility."
        )
    elif tier is AutonomyTier.DRAFT_FOR_APPROVAL:
        reason = state.decision.reason if state.decision else "vendor not approved"
        subject = f"[Action required] Approve draft PO for {sku}"
        body = (
            f"Hi {recipient_name}, {sku} ({desc}) breached its reorder point "
            f"({state.stock.on_hand}/{state.stock.reorder_threshold}) and needs "
            f"~{qty} units, but it cannot be auto-issued ({reason}). A draft PO "
            f"to {vendor} is awaiting your approval before anything is sent."
        )
    else:  # SUPPRESS
        cover = state.forecast.weeks_of_cover if state.forecast else None
        subject = f"[No action] {sku} reorder suppressed — inbound supply covers it"
        body = (
            f"Hi {recipient_name}, {sku} ({desc}) is below its reorder point on "
            f"hand, but in-transit supply already covers the threshold "
            f"(effective {state.stock.effective_stock}"
            + (f", ~{cover:.1f} weeks cover" if isinstance(cover, (int, float)) else "")
            + "). No new PO was created; logged for audit."
        )
    return subject, body


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(
    state: "ReplenishmentState", session: Session | None = None
) -> "ReplenishmentState":
    """Compose, persist, and audit the DP notification for ``state.sku``.

    Args:
        state: Shared state carrying the routing decision (and PO, if any).
        session: Optional shared session; one is opened/closed if omitted.

    Returns:
        The same state with ``state.notification`` populated.

    Rationale: the pipeline's final node — turns a decision into a DP-facing
        message and records that it was sent.
    """
    own = session is None
    s = session or get_session()
    try:
        category = state.stock.category if state.stock else None
        dp = db.dp_for_category(s, category) if category else None
        recipient_name = dp.name if dp else "Demand Planner"

        result = compose(state, recipient_name)
        s.add(
            Notification(
                recipient=dp.slack_handle if (dp and result.channel is Channel.SLACK) else (dp.email if dp else recipient_name),
                channel=result.channel.value,
                subject=result.subject,
                body=result.body,
                sku=result.sku,
                po_number=result.po_number,
                autonomy_tier=result.autonomy_tier.value if result.autonomy_tier else None,
                status=result.status,
                created_at=datetime.now(),
            )
        )
        db.append_audit(
            s,
            run_id=state.run_id,
            agent=AGENT_NAME,
            event_type="NOTIFIED",
            sku=result.sku,
            autonomy_tier=result.autonomy_tier.value if result.autonomy_tier else None,
            po_number=result.po_number,
            summary=f"{result.sku}: notified {recipient_name} via {result.channel.value} ({result.urgency.value}).",
            details=result.model_dump(mode="json"),
        )
        s.commit()
        state.notification = result
        log.info("%s -> notified %s via %s", state.sku, recipient_name, result.channel.value)
        return state
    finally:
        if own:
            s.close()
