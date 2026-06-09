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
    log.info("run_for_sku %s -> %s (%s)", sku, outcome.status, outcome.route)
    return outcome


def resume_run(
    thread_id: str,
    *,
    approved: bool,
    approved_by: str | None = None,
    note: str | None = None,
) -> RunOutcome:
    """Resume a paused draft run with a human decision.

    Args:
        thread_id: The thread id of the paused run (from its :class:`RunOutcome`).
        approved: Whether the human approves the draft PO.
        approved_by: Approver handle, recorded on the PO if written.
        note: Optional reviewer note.

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
    decision = HumanDecision(approved=approved, approved_by=approved_by, note=note)
    graph.invoke(Command(resume=decision.model_dump()), config=config)
    outcome = _outcome_from_state(run_id, thread_id, sku)
    log.info(
        "resume_run %s approved=%s -> %s (PO %s)",
        sku, approved, outcome.status, outcome.po_number,
    )
    return outcome


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
