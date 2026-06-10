"""Execution helpers that drive the replenishment graph for the demo.

Three entry points:

* :func:`run_for_sku` — run one SKU end to end. Auto-issue and suppress runs
  finish; a draft run *pauses* at the human-approval interrupt and returns a
  :class:`RunOutcome` carrying the :class:`~orchestration.state.ApprovalRequest`.
* :func:`resume_run` — resume a paused draft thread with a human decision; the
  PO is written (status APPROVED) only if the decision approves it.
* :func:`run_batch` — the Scenario-3 batch: scan a set of SKUs, group the
  auto-issue ones by shared approved vendor into *consolidated* POs (one PO per
  vendor, many lines), and route the suspended-vendor SKU through the draft path
  so it comes back paused.

A single compiled graph (with one in-memory checkpointer) is cached for the
process so a thread paused by :func:`run_for_sku` can be resumed by
:func:`resume_run` using its ``thread_id``. Every agent writes its own audit
rows as it runs, so each of these calls leaves a complete trail behind it.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from agents import (
    approval,
    demand_forecast,
    notification,
    po_generator,
    stock_monitor,
    vendor_checker,
)
from agents.enums import AutonomyTier
from agents.mock_blue_yonder import POLineSpec
from agents.po_generator import POResult
from agents.state import ReplenishmentState, new_state
from data.database import get_session
from orchestration.graph import build_graph
from orchestration.state import GraphState, HumanDecision, new_graph_input

log = logging.getLogger("orchestration.runner")

# Compiled graph cached for the process. One checkpointer instance is what lets
# a paused draft thread be resumed later by thread_id.
_GRAPH = None

# Maps a paused run_id -> its checkpointer thread_id, so approve()/reject() can
# resume by the run_id a caller already holds (the demo/dashboard key on run_id).
# Populated whenever run_for_sku leaves a run paused at the human gate.
_PAUSED_THREADS: dict[str, str] = {}


def get_graph():
    """Return the process-wide compiled graph, building it on first use.

    Returns:
        The cached compiled graph (shared in-memory checkpointer).

    Rationale: run/resume must share one checkpointer; a module-level singleton
        guarantees that without threading the graph through every call site.
    """
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def reset_graph() -> None:
    """Discard the cached graph and the paused-thread registry.

    Rationale: the dashboard's "Run 6 AM scan" starts the day fresh — it re-seeds
        the database, so the in-memory checkpointer (which still holds the prior
        scan's paused threads) must be dropped too, or a stale thread_id could
        resume against data that no longer exists.
    """
    global _GRAPH
    _GRAPH = None
    _PAUSED_THREADS.clear()


def snapshot(thread_id: str) -> dict[str, Any]:
    """Return the full checkpointed graph state for a run, for display.

    Args:
        thread_id: The checkpointer thread id of a completed or paused run.

    Returns:
        The graph's channel values (``stock`` / ``forecast`` / ``vendor`` /
        ``decision`` / ``po`` / ``notification`` / HITL flags), or ``{}`` if the
        thread is unknown.

    Rationale: :class:`RunOutcome` is a flat summary; the dashboard cards need the
        rich per-agent verdicts, and the checkpointer already holds them — so the
        UI reads them here rather than re-running any agent (which would re-audit).
    """
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    return dict(snap.values)


# ---------------------------------------------------------------------------
# Outcome models
# ---------------------------------------------------------------------------
class RunOutcome(BaseModel):
    """The result of a single-SKU run (completed or paused at the interrupt)."""

    run_id: str
    thread_id: str
    sku: str
    status: str = Field(..., description="'completed' or 'paused'.")
    route: AutonomyTier | None
    paused_for_approval: bool
    approval_request: dict[str, Any] | None = Field(
        None, description="The interrupt payload when paused; else None."
    )
    po_number: str | None = None
    po_written: bool = False
    notification_subject: str | None = None
    approval_deadline: str | None = Field(
        None, description="ISO SLA deadline surfaced for a CRITICAL-LOW draft, if any."
    )
    human_approved: bool | None = None
    rejection_reason: str | None = None
    alternate_sourcing: bool = Field(
        False, description="True once a draft was rejected (procurement to source elsewhere)."
    )


class ConsolidatedPO(BaseModel):
    """One vendor-consolidated PO produced by the batch run."""

    po_number: str | None
    vendor_id: str
    skus: list[str]
    line_count: int
    total_cost: float


class BatchOutcome(BaseModel):
    """The result of a batch run (the Scenario-3 seasonal surge)."""

    run_id: str
    scanned: list[str]
    auto_issue_skus: list[str]
    consolidated_pos: list[ConsolidatedPO]
    suppressed_skus: list[str]
    draft_runs: list[RunOutcome] = Field(
        default_factory=list, description="Paused draft runs (e.g. SKU-708)."
    )


# ---------------------------------------------------------------------------
# Single-SKU execution
# ---------------------------------------------------------------------------
def _outcome_from_state(run_id: str, thread_id: str, sku: str) -> RunOutcome:
    """Read the checkpointed graph state and summarise it as a RunOutcome.

    Args:
        run_id: The run correlation id.
        thread_id: The checkpointer thread id (used to fetch state).
        sku: The SKU processed.

    Returns:
        A populated :class:`RunOutcome` reflecting completed vs paused.

    Rationale: one place maps LangGraph's snapshot (values + pending tasks +
        interrupts) onto the flat outcome the demo and validator consume.
    """
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    values = snap.values
    paused = bool(snap.next)

    request: dict[str, Any] | None = None
    if paused:
        for task in snap.tasks:
            for itr in task.interrupts:
                request = itr.value
                break
            if request is not None:
                break

    po = values.get("po")
    notif = values.get("notification")
    route = values.get("route")
    decision = values.get("decision")
    # Prefer the live decision's deadline; fall back to the paused interrupt payload.
    deadline = getattr(decision, "approval_deadline", None) or (
        (request or {}).get("approval_deadline") if request else None
    )
    return RunOutcome(
        run_id=run_id,
        thread_id=thread_id,
        sku=sku,
        status="paused" if paused else "completed",
        route=route,
        paused_for_approval=paused,
        approval_request=request,
        po_number=po.po_number if po else None,
        po_written=bool(po.written) if po else False,
        notification_subject=notif.subject if notif else None,
        approval_deadline=deadline,
        human_approved=values.get("human_approved"),
        rejection_reason=values.get("rejection_reason"),
        alternate_sourcing=bool(values.get("alternate_sourcing")),
    )


def run_for_sku(sku: str, run_id: str | None = None, thread_id: str | None = None) -> RunOutcome:
    """Run one SKU through the graph to completion or to the draft pause.

    Args:
        sku: The SKU to process.
        run_id: Optional correlation id; defaults to ``run-<sku>``.
        thread_id: Optional checkpointer thread id; defaults to ``run_id``.

    Returns:
        A :class:`RunOutcome`. Auto-issue and suppress runs are ``completed``;
        a draft run is ``paused`` with its :class:`ApprovalRequest` attached.

    Rationale: the demo's primary verb — proves S1 completes, S2 pauses, and S4
        suppresses, all through the same graph with no per-scenario branching.
    """
    rid = run_id or f"run-{sku}"
    tid = thread_id or rid
    graph = get_graph()
    config = {"configurable": {"thread_id": tid}}
    graph.invoke(new_graph_input(sku, rid), config=config)
    outcome = _outcome_from_state(rid, tid, sku)
    if outcome.paused_for_approval:
        # Remember where this run paused so approve(run_id) / reject(run_id) can
        # resume it without the caller having to track the thread_id separately.
        _PAUSED_THREADS[rid] = tid
    log.info("run_for_sku %s -> %s (%s)", sku, outcome.status, outcome.route)
    return outcome


def resume_run(
    thread_id: str,
    *,
    approved: bool,
    approved_by: str | None = None,
    note: str | None = None,
    reason: str | None = None,
) -> RunOutcome:
    """Resume a paused draft run with a human decision.

    Args:
        thread_id: The thread id of the paused run (from its :class:`RunOutcome`).
        approved: Whether the human approves the draft PO.
        approved_by: Approver/rejecter handle, recorded on the PO if written.
        note: Optional reviewer note (approval path).
        reason: Rejection reason (reject path); flags alternate sourcing.

    Returns:
        The completed :class:`RunOutcome`. The PO is written (status APPROVED)
        only when ``approved`` is True; a rejection writes nothing.

    Raises:
        ValueError: If the thread is not actually paused (nothing to resume).

    Rationale: the second half of the human-in-the-loop — resuming unblocks the
        PO node, where the guardrail then writes only on an approving decision.
    """
    from langgraph.types import Command  # local import: only needed on resume.

    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(config)
    if not snap.next:
        raise ValueError(f"Thread {thread_id!r} is not paused; nothing to resume.")

    sku = snap.values.get("sku")
    run_id = snap.values.get("run_id")
    decision = HumanDecision(
        approved=approved, approved_by=approved_by, note=note, reason=reason
    )
    graph.invoke(Command(resume=decision.model_dump()), config=config)
    _PAUSED_THREADS.pop(run_id, None)  # no longer paused.
    outcome = _outcome_from_state(run_id, thread_id, sku)
    log.info(
        "resume_run %s approved=%s -> %s (PO %s)",
        sku, approved, outcome.status, outcome.po_number,
    )
    return outcome


def _thread_for(run_id: str) -> str:
    """Resolve the checkpointer thread id for a paused run_id.

    Args:
        run_id: The run correlation id a caller holds.

    Returns:
        The thread id to resume. Falls back to ``run_id`` itself, since
        single-SKU runs use ``thread_id == run_id`` by convention.

    Rationale: approve()/reject() take the run_id the dashboard already shows; the
        registry hides the run_id->thread_id mapping behind that public verb.
    """
    return _PAUSED_THREADS.get(run_id, run_id)


def approve(run_id: str, decision: str | None = None, note: str | None = None) -> RunOutcome:
    """Approve a paused draft run; the PO Generator then writes the PO.

    Args:
        run_id: The paused run's correlation id (as shown to the planner).
        decision: The approver's handle/identity (who signed off).
        note: Optional free-text approval note, recorded on the PO and audit.

    Returns:
        The completed :class:`RunOutcome` carrying exactly one written, APPROVED PO.

    Raises:
        ValueError: If the run is not paused awaiting approval.

    Rationale: the human-in-the-loop "yes" — the only way a draft tier ever
        reaches Blue Yonder; resuming clears the interrupt and the guardrail in
        the PO node permits the write because ``human_approved`` is now True.
    """
    return resume_run(
        _thread_for(run_id), approved=True, approved_by=decision, note=note
    )


def reject(run_id: str, reason: str, decision: str | None = None) -> RunOutcome:
    """Reject a paused draft run; no PO is written and procurement is notified.

    Args:
        run_id: The paused run's correlation id.
        reason: Why the draft is rejected — logged and shown to procurement.
        decision: Optional handle/identity of the rejecter.

    Returns:
        The completed :class:`RunOutcome`: no PO, ``alternate_sourcing`` set, and
        a procurement notification recorded.

    Raises:
        ValueError: If the run is not paused awaiting approval.

    Rationale: the human-in-the-loop "no" — the guardrail keeps Blue Yonder
        untouched, the rejection reason is written to the immutable audit trail,
        the alternate-sourcing flag is raised, and procurement is told to source
        the SKU another way.
    """
    return resume_run(
        _thread_for(run_id), approved=False, approved_by=decision, reason=reason
    )


# ---------------------------------------------------------------------------
# Batch execution (Scenario 3)
# ---------------------------------------------------------------------------
def _assess(sku: str, session) -> ReplenishmentState:
    """Run the first four agents for a SKU and return the routed state.

    Args:
        sku: The SKU to assess.
        session: Shared ORM session (the four agents reuse one transaction).

    Returns:
        A :class:`ReplenishmentState` with stock, forecast, vendor, and decision
        populated — but no PO or notification yet.

    Rationale: the batch must know each SKU's tier *before* it can consolidate
        the auto-issue ones, so detection/routing is separated from execution.
    """
    state = new_state(sku, run_id=f"batch-{sku}")
    stock_monitor.run(state, session)
    demand_forecast.run(state, session)
    vendor_checker.run(state, session)
    approval.run(state, session)
    return state


def run_batch(skus: list[str], run_id: str | None = None) -> BatchOutcome:
    """Scan a set of SKUs and execute the Scenario-3 consolidation.

    Args:
        skus: The SKUs to scan (e.g. the Confectionery SKU-701..708 batch).
        run_id: Optional batch correlation id.

    Returns:
        A :class:`BatchOutcome` listing the consolidated POs (one per shared
        approved vendor), the suppressed SKUs, and any paused draft runs.

    Rationale: consolidation is a *batch* property — grouping auto-issue SKUs by
        vendor turns N per-SKU orders into one order per vendor, while a
        suspended-vendor SKU still falls out to the paused draft path.
    """
    rid = run_id or "batch-run"
    states: list[ReplenishmentState] = []
    with get_session() as session:
        for sku in skus:
            states.append(_assess(sku, session))
        session.commit()

        auto = [s for s in states if s.decision.tier is AutonomyTier.AUTO_ISSUE]
        drafts = [s for s in states if s.decision.tier is AutonomyTier.DRAFT_FOR_APPROVAL]
        suppressed = [s for s in states if s.decision.tier is AutonomyTier.SUPPRESS]
        by_sku = {s.sku: s for s in states}

        # Group auto-issue SKUs by their shared approved vendor -> one PO each.
        groups = vendor_checker.consolidate_by_vendor([s.vendor for s in auto])
        consolidated: list[ConsolidatedPO] = []
        for vendor_id, assessments in groups.items():
            lines = [
                POLineSpec(
                    sku=a.sku,
                    quantity=by_sku[a.sku].forecast.qty_needed,
                    unit_cost=a.unit_cost or 0.0,
                )
                for a in assessments
            ]
            po = po_generator.generate_consolidated(
                session, run_id=rid, vendor_id=vendor_id, lines=lines
            )
            consolidated.append(
                ConsolidatedPO(
                    po_number=po.po_number,
                    vendor_id=vendor_id,
                    skus=[a.sku for a in assessments],
                    line_count=len(po.lines),
                    total_cost=po.total_cost,
                )
            )
            # Notify each SKU's DP that it shipped on the consolidated PO.
            _notify_each(session, [by_sku[a.sku] for a in assessments], po)

        # Suppressed SKUs: advisory notification only, no PO.
        for s in suppressed:
            notification.run(s, session)
        session.commit()

    # Draft SKUs go through the real graph so they come back paused (S3: SKU-708).
    draft_runs = [
        run_for_sku(s.sku, run_id=f"batch-{s.sku}", thread_id=f"batch-{s.sku}")
        for s in drafts
    ]

    outcome = BatchOutcome(
        run_id=rid,
        scanned=[s.sku for s in states],
        auto_issue_skus=[s.sku for s in auto],
        consolidated_pos=consolidated,
        suppressed_skus=[s.sku for s in suppressed],
        draft_runs=draft_runs,
    )
    log.info(
        "run_batch -> %d consolidated PO(s), %d draft(s), %d suppressed",
        len(consolidated), len(draft_runs), len(suppressed),
    )
    return outcome


def _notify_each(session, states: list[ReplenishmentState], po: POResult) -> None:
    """Send each SKU's DP the auto-issue notification for a consolidated PO.

    Args:
        session: Open ORM session.
        states: The per-SKU states covered by the consolidated PO.
        po: The written consolidated :class:`POResult` (shared PO number).

    Rationale: a consolidated PO still concerns several SKUs across (possibly)
        different planners, so each owner is notified, all referencing one PO.
    """
    for s in states:
        s.po = po
        notification.run(s, session)
