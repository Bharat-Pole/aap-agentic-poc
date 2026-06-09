"""The typed state object that flows through the LangGraph replenishment graph.

Phase 1 already defines :class:`agents.state.ReplenishmentState` — the single
object every agent reads from and writes to. The orchestration layer reuses it
verbatim and only adds the one field a *graph* needs that a linear pipeline did
not: an explicit, hoisted ``route``. The conditional edge after the Approval
Agent keys on ``state.route`` (a mirror of ``decision.tier``) so the fork is a
single, obvious field read rather than a reach into a nested model — and so the
dashboard can colour a run by its route without unpacking the decision.

The phase's required state inventory maps onto this object as:

    sku context        -> ReplenishmentState.sku / .stock
    stock result       -> .stock
    forecast           -> .forecast
    vendor result      -> .vendor
    route              -> GraphState.route        (added here)
    justification      -> .decision.justification_payload
    approval decision  -> .decision
    human_approved     -> .human_approved / .approved_by
    po result          -> .po
    notification       -> .notification
    run_id             -> .run_id

Two small models describe the human-in-the-loop boundary: :class:`ApprovalRequest`
is the payload the draft path surfaces through LangGraph's ``interrupt`` (what a
human is asked to rule on); :class:`HumanDecision` is the value passed back in to
resume the paused graph.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.enums import AutonomyTier
from agents.state import ReplenishmentState


class GraphState(ReplenishmentState):
    """Shared pipeline state, extended with the hoisted routing field.

    Inherits every per-agent slot from :class:`ReplenishmentState`; agents type
    against the base class and accept this subclass transparently. The single
    addition is :attr:`route`, set by the approval node and read by the
    post-approval conditional edge.
    """

    route: AutonomyTier | None = Field(
        None,
        description="Autonomy tier the graph branches on; mirror of decision.tier.",
    )


class ApprovalRequest(BaseModel):
    """The payload surfaced to a human when a draft pauses the graph.

    Carries only the deterministic facts a planner needs to rule on a draft PO,
    so the approval surface (Streamlit, or the validation harness) never has to
    re-query. Emitted as the value of LangGraph's ``interrupt`` call.
    """

    run_id: str
    sku: str
    vendor_id: str | None
    qty_needed: int | None
    reason: str = Field(..., description="Why this SKU could not be auto-issued.")
    draft_justification: dict[str, Any] | None = Field(
        None, description="Structured justification payload from the Approval Agent."
    )


class HumanDecision(BaseModel):
    """The value passed back in to resume a paused (draft) run.

    Resuming the graph with ``Command(resume=HumanDecision(...).model_dump())``
    is what the ``interrupt`` call inside the human-approval node returns.
    """

    approved: bool = Field(..., description="True to approve the draft PO, False to reject.")
    approved_by: str | None = Field(
        None, description="Handle of the approver; recorded on the written PO."
    )
    note: str | None = Field(None, description="Optional free-text reviewer note.")


def new_graph_input(sku: str, run_id: str | None = None) -> dict[str, Any]:
    """Build the initial channel values for a single-SKU graph invocation.

    Args:
        sku: The SKU to process.
        run_id: Optional correlation id; defaults to ``run-<sku>``.

    Returns:
        A dict with the two required state fields populated; every other slot
        defaults to ``None``/empty and is filled by the agents as they run.

    Rationale: ``invoke`` wants the initial channel values, not a full model —
        keeping construction here means the runner never hand-builds that dict.
    """
    return {"run_id": run_id or f"run-{sku}", "sku": sku}
