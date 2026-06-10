"""The LangGraph state graph that wires the six agents together.

Topology (linear prefix, then a three-way fork keyed on the autonomy tier)::

    START
      -> stock_monitor -> demand_forecast -> vendor_checker -> approval
      -> {route}
           AUTO-ISSUE         -> po_generator -> notification -> END
           DRAFT-FOR-APPROVAL -> human_approval -> po_generator -> notification -> END
           SUPPRESS           -> notification -> END        (no po_generator)

The ``human_approval`` node calls LangGraph's :func:`interrupt`, which *pauses*
the whole run and surfaces an :class:`~orchestration.state.ApprovalRequest` to
the caller. Nothing downstream — crucially the PO write — executes until the run
is resumed with a human decision, giving us the human-in-the-loop guardrail for
free: the draft path physically cannot reach ``po_generator`` without a resume.

Each node is a thin wrapper over a Phase-1 agent's ``run(state)`` function. The
agents already open their own session, write their audit row, and commit, so the
full audit trail is produced by simply letting every node run — no orchestration
-level audit code, and no DB session held across the interrupt (which would not
survive a pause/resume). The compiled graph uses an in-memory checkpointer so a
paused thread can be resumed by ``thread_id`` within the process.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from agents import (
    approval,
    db,
    demand_forecast,
    notification,
    po_generator,
    stock_monitor,
    vendor_checker,
)
from agents.enums import AutonomyTier
from data.database import get_session
from orchestration.state import ApprovalRequest, GraphState, HumanDecision

log = logging.getLogger("orchestration.graph")

# Node names — referenced by the routing function, the docs, and the runner.
N_STOCK = "stock_monitor"
N_FORECAST = "demand_forecast"
N_VENDOR = "vendor_checker"
N_APPROVAL = "approval"
N_HUMAN = "human_approval"
N_PO = "po_generator"
N_NOTIFY = "notification"


# ---------------------------------------------------------------------------
# Nodes — each wraps one agent's run() and returns only the fields it filled.
# ---------------------------------------------------------------------------
def _stock_node(state: GraphState) -> dict:
    """Run the Stock Monitor and publish its verdict.

    Args:
        state: Current graph state (its ``sku`` selects the target).

    Returns:
        The ``stock`` channel update.

    Rationale: first node — turns a SKU into a classified breach/suppress signal.
    """
    stock_monitor.run(state)
    return {"stock": state.stock}


def _forecast_node(state: GraphState) -> dict:
    """Run the Demand Forecast Agent and publish the sizing.

    Args:
        state: Graph state carrying ``stock`` (selects advisory vs order mode).

    Returns:
        The ``forecast`` channel update.

    Rationale: sizes the order (or reports cover on the suppress path).
    """
    demand_forecast.run(state)
    return {"forecast": state.forecast}


def _vendor_node(state: GraphState) -> dict:
    """Run the Vendor Checker and publish the supplier assessment.

    Args:
        state: Graph state carrying ``forecast`` (qty tested against MOQ).

    Returns:
        The ``vendor`` channel update.

    Rationale: recommends auto-issue vs draft from vendor standing + MOQ.
    """
    vendor_checker.run(state)
    return {"vendor": state.vendor}


def _approval_node(state: GraphState) -> dict:
    """Run the Approval Agent and publish the decision plus the hoisted route.

    Args:
        state: Graph state carrying stock, forecast, and vendor verdicts.

    Returns:
        The ``decision`` and ``route`` channel updates; ``route`` mirrors
        ``decision.tier`` so the conditional edge reads a single field.

    Rationale: the critical routing node — its tier decides the whole branch.
    """
    approval.run(state)
    return {"decision": state.decision, "route": state.decision.tier}


def _human_approval_node(state: GraphState) -> dict:
    """Pause the run and await a human ruling on the draft (human-in-the-loop).

    Args:
        state: Graph state carrying the draft decision and justification payload.

    Returns:
        The HITL channel updates (``human_approved`` / ``approved_by`` /
        ``human_note`` / ``rejection_reason`` / ``alternate_sourcing``), set from
        the decision the caller resumes with.

    Rationale: :func:`interrupt` suspends the graph *before* any PO write, so a
        draft can never reach Blue Yonder without an explicit human resume — the
        guardrail enforced structurally by the graph, not just by convention. The
        decision is committed to the audit trail here (the body after the
        interrupt runs exactly once, on resume) so every human ruling is evidence.
    """
    dec = state.decision
    request = ApprovalRequest(
        run_id=state.run_id,
        sku=state.sku,
        vendor_id=state.vendor.primary_vendor_id if state.vendor else None,
        qty_needed=state.forecast.qty_needed if state.forecast else None,
        reason=dec.reason if dec else "vendor not approved",
        is_critical=dec.is_critical if dec else False,
        approval_deadline=dec.approval_deadline if dec else None,
        draft_justification=dec.justification_payload if dec else None,
        draft_narrative=dec.justification_narrative if dec else None,
    )
    # Execution stops here until the run is resumed with Command(resume=...).
    raw = interrupt(request.model_dump())
    decision = raw if isinstance(raw, HumanDecision) else HumanDecision.model_validate(raw)
    alternate_sourcing = not decision.approved
    log.info(
        "%s -> human decision: approved=%s by=%s",
        state.sku, decision.approved, decision.approved_by,
    )
    _audit_human_decision(state, decision, alternate_sourcing)
    return {
        "human_approved": decision.approved,
        "approved_by": decision.approved_by,
        "human_note": decision.note,
        "rejection_reason": decision.reason if not decision.approved else None,
        "alternate_sourcing": alternate_sourcing,
    }


def _audit_human_decision(
    state: GraphState, decision: HumanDecision, alternate_sourcing: bool
) -> None:
    """Append the human approve/reject ruling to the immutable audit trail.

    Args:
        state: Graph state for the paused draft (supplies run_id / sku / vendor).
        decision: The :class:`HumanDecision` the run was resumed with.
        alternate_sourcing: Whether the rejection flagged alternate sourcing.

    Rationale: the human gate is a first-class decision event — recorded with the
        approver and their note (approve) or reason (reject) so the trail shows
        exactly who unblocked, or blocked, each draft PO.
    """
    approved = decision.approved
    event_type = "APPROVAL_GRANTED" if approved else "APPROVAL_REJECTED"
    if approved:
        summary = (
            f"{state.sku}: draft APPROVED by {decision.approved_by or 'unknown'}"
            + (f" — {decision.note}" if decision.note else "")
        )
    else:
        summary = (
            f"{state.sku}: draft REJECTED by {decision.approved_by or 'unknown'}"
            f" — {decision.reason or 'no reason given'}; alternate sourcing flagged."
        )
    with get_session() as s:
        db.append_audit(
            s,
            run_id=state.run_id,
            agent="HumanApproval",
            event_type=event_type,
            sku=state.sku,
            vendor_id=state.vendor.primary_vendor_id if state.vendor else None,
            autonomy_tier=AutonomyTier.DRAFT_FOR_APPROVAL.value,
            summary=summary,
            details={
                "approved": approved,
                "approved_by": decision.approved_by,
                "note": decision.note,
                "rejection_reason": decision.reason if not approved else None,
                "alternate_sourcing": alternate_sourcing,
            },
        )
        s.commit()


def _po_node(state: GraphState) -> dict:
    """Run the PO Generator (the only node that can write to Blue Yonder).

    Args:
        state: Graph state carrying the routing decision and approval flags.

    Returns:
        The ``po`` channel update (written, or skipped with a reason).

    Rationale: the guardrail lives inside the agent — auto-issue writes; an
        approved draft writes; an unapproved draft or suppress writes nothing.
    """
    po_generator.run(state)
    return {"po": state.po}


def _notify_node(state: GraphState) -> dict:
    """Run the Notification Agent and publish the DP message.

    Args:
        state: Graph state carrying the decision and (optional) PO result.

    Returns:
        The ``notification`` channel update.

    Rationale: terminal node on every branch — the DP always hears the outcome.
    """
    notification.run(state)
    return {"notification": state.notification}


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------
def route_after_approval(state: GraphState) -> str:
    """Pick the next node from the autonomy tier the Approval Agent assigned.

    Args:
        state: Graph state whose ``route`` was set by the approval node.

    Returns:
        The name of the next node:
        ``po_generator`` (auto-issue), ``human_approval`` (draft), or
        ``notification`` (suppress — no PO is ever generated).

    Rationale: this single function is the autonomy fork; suppress skips the PO
        node entirely so the guardrail cannot be reached on a false alarm.
    """
    tier = state.route or (state.decision.tier if state.decision else None)
    if tier is AutonomyTier.AUTO_ISSUE:
        return N_PO
    if tier is AutonomyTier.DRAFT_FOR_APPROVAL:
        return N_HUMAN
    return N_NOTIFY


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def build_graph(checkpointer: MemorySaver | None = None) -> CompiledStateGraph:
    """Build and compile the six-agent replenishment graph.

    Args:
        checkpointer: Optional checkpointer; a fresh :class:`MemorySaver` is
            created if omitted. A checkpointer is required for the draft path's
            interrupt/resume to work (it persists the paused thread).

    Returns:
        The compiled, runnable graph.

    Rationale: one builder keeps the topology in a single place; the interrupt
        is expressed inside the human-approval node rather than via static
        ``interrupt_before`` so the pause carries a structured request payload.
    """
    g: StateGraph = StateGraph(GraphState)

    g.add_node(N_STOCK, _stock_node)
    g.add_node(N_FORECAST, _forecast_node)
    g.add_node(N_VENDOR, _vendor_node)
    g.add_node(N_APPROVAL, _approval_node)
    g.add_node(N_HUMAN, _human_approval_node)
    g.add_node(N_PO, _po_node)
    g.add_node(N_NOTIFY, _notify_node)

    # Linear prefix: detect -> size -> vendor -> route.
    g.add_edge(START, N_STOCK)
    g.add_edge(N_STOCK, N_FORECAST)
    g.add_edge(N_FORECAST, N_VENDOR)
    g.add_edge(N_VENDOR, N_APPROVAL)

    # Three-way autonomy fork.
    g.add_conditional_edges(
        N_APPROVAL,
        route_after_approval,
        {N_PO: N_PO, N_HUMAN: N_HUMAN, N_NOTIFY: N_NOTIFY},
    )

    # Draft path rejoins the auto path after the human gate.
    g.add_edge(N_HUMAN, N_PO)
    g.add_edge(N_PO, N_NOTIFY)
    g.add_edge(N_NOTIFY, END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
