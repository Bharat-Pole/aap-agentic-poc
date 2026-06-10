"""The dashboard's bridge to the orchestration runner.

``app.py`` never calls an agent or touches LangGraph directly; it goes through
the verbs here, which wrap :mod:`orchestration.runner` (``run_for_sku`` /
``run_batch`` / ``approve`` / ``reject``) and flatten the rich pipeline state into
typed :class:`WorklistCard` objects the UI can render without unpacking nested
models. Detection, forecasting, and routing all stay in the deterministic agents
— this layer only *orchestrates and presents* them.

The scan is the demo's reset point: :func:`run_scan` re-seeds the database (so the
Blue Yonder table starts empty and the four scenarios reproduce byte-for-byte)
and drops the in-memory graph, then executes all four scenarios. S1 auto-issues
and S3 consolidates immediately (their POs appear at once); S2 and S3's SKU-708
pause for a human; S4 suppresses. The returned :class:`ScanResult` is held in
Streamlit's server-side session state — no client storage is involved.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

import config
from agents.enums import AutonomyTier
from agents.stock_monitor import StockStatus, scan_all
from data.database import get_session
from orchestration import runner
from orchestration.runner import BatchOutcome, RunOutcome
from scripts.seed_data import seed
from ui.scenarios import SCENARIOS, S3_SKUS, scenario_for_sku

log = logging.getLogger("ui.service")


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------
class DetectionItem(BaseModel):
    """One row of the 6 AM scan's detection list."""

    sku: str
    scenario: str = Field(..., description="Owning scenario id, or '—'.")
    description: str
    signal: str = Field(..., description="StockSignal value, e.g. 'CRITICAL-LOW'.")
    on_hand: int
    reorder_threshold: int
    effective_stock: int
    note: str


class WorklistCard(BaseModel):
    """A flattened, render-ready summary of one pipeline result.

    One card is one demo-meaningful unit: a single-SKU run (S1/S2/S4), one
    consolidated PO from the S3 batch, or the S3 SKU-708 escalation. The ``route``
    drives the colour-coded badge; the optional draft fields populate the
    approval surface; ``run_id`` is what approve/reject act on.
    """

    scenario: str = Field(..., description="Scenario id, e.g. 'S3'.")
    kind: str = Field(
        ..., description="auto | draft | suppress | consolidated — selects the card layout."
    )
    title: str
    skus: list[str] = Field(default_factory=list)
    route: AutonomyTier | None = None
    route_label: str = Field(..., description="Badge text: AUTO-ISSUED / NEEDS APPROVAL / SUPPRESSED.")

    # Stock + forecast context (single-SKU cards).
    description: str | None = None
    on_hand: int | None = None
    reorder_threshold: int | None = None
    effective_stock: int | None = None
    signal: str | None = None
    forecast_qty: int | None = None
    forecast_basis: str | None = None
    weeks_of_cover: float | None = None

    # Vendor context.
    vendor_id: str | None = None
    vendor_status: str | None = None
    approval_status: str | None = None

    # PO context (written cards).
    po_number: str | None = None
    po_status: str | None = None
    po_written: bool = False
    total_cost: float | None = None
    line_skus: list[str] = Field(default_factory=list, description="SKUs on a consolidated PO.")

    # Notification.
    notification_subject: str | None = None

    # Draft / human-in-the-loop fields.
    run_id: str | None = Field(None, description="Run id approve()/reject() act on.")
    draft_narrative: str | None = None
    draft_reason: str | None = None
    is_critical: bool = False
    approval_deadline: str | None = None
    chart_sku: str | None = Field(
        None, description="SKU whose 90-day offtake the card charts (draft cards)."
    )
    decided: bool = Field(False, description="True once a human has ruled on a draft.")
    decision_label: str | None = Field(
        None, description="'Approved' / 'Rejected' after a human ruling."
    )


class ScanResult(BaseModel):
    """Everything one 6 AM scan produced — the unit held in session state."""

    scan_id: int
    detection: list[DetectionItem]
    cards: list[WorklistCard]

    def signal_counts(self) -> dict[str, int]:
        """Tally the detection list by stock signal.

        Returns:
            ``{signal_value: count}`` over the flagged SKUs.

        Rationale: feeds the scan's "low / critical / suppressed" metric strip.
        """
        counts: dict[str, int] = {}
        for item in self.detection:
            counts[item.signal] = counts.get(item.signal, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Badge mapping
# ---------------------------------------------------------------------------
#: Route -> the planner-facing badge label (matches the master spec wording).
ROUTE_LABEL: dict[AutonomyTier, str] = {
    AutonomyTier.AUTO_ISSUE: "AUTO-ISSUED",
    AutonomyTier.DRAFT_FOR_APPROVAL: "NEEDS APPROVAL",
    AutonomyTier.SUPPRESS: "SUPPRESSED",
}


def route_label(route: AutonomyTier | None) -> str:
    """Map an autonomy tier to its dashboard badge text.

    Args:
        route: The tier, or ``None`` (an in-flight/unknown run).

    Returns:
        The badge label; ``'—'`` when the route is unknown.

    Rationale: one mapping keeps the badge wording identical across every card.
    """
    return ROUTE_LABEL.get(route, "—") if route else "—"


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def run_scan(scan_id: int) -> ScanResult:
    """Re-seed, scan the warehouse, and execute all four scenarios.

    Args:
        scan_id: A monotonically increasing scan counter (used to scope run ids so
            successive scans never collide in the checkpointer).

    Returns:
        A :class:`ScanResult` with the detection list and the worklist cards.

    Rationale: this is the demo's primary action — one click that reproduces the
        whole morning run deterministically, leaving auto-issue and consolidated
        POs already in Blue Yonder and the draft cases paused for a human.
    """
    log.info("run_scan #%d: re-seeding and executing scenarios", scan_id)
    seed()  # clean, deterministic slate: empties Blue Yonder + audit + notifications.
    runner.reset_graph()  # drop the prior scan's paused threads from the checkpointer.

    detection = _detect()
    cards: list[WorklistCard] = []
    for sc in SCENARIOS:
        if sc.kind == "batch":
            cards.extend(_run_batch_scenario(sc.id, scan_id))
        else:
            cards.append(_run_single_scenario(sc.id, sc.skus[0], scan_id))
    return ScanResult(scan_id=scan_id, detection=detection, cards=cards)


def _detect() -> list[DetectionItem]:
    """Run the Stock Monitor across the whole warehouse and list what it flagged.

    Returns:
        A :class:`DetectionItem` per non-healthy SKU, scenario-tagged, in SKU order.

    Rationale: the scan's headline output — proves only genuine breaches and
        suppression candidates surface, and the ~40 healthy SKUs are ignored.
    """
    with get_session() as s:
        flagged: list[StockStatus] = scan_all(s)  # run_id omitted: these are scan rows.
        s.commit()
    return [
        DetectionItem(
            sku=st.sku,
            scenario=scenario_for_sku(st.sku),
            description=st.description,
            signal=st.signal.value,
            on_hand=st.on_hand,
            reorder_threshold=st.reorder_threshold,
            effective_stock=st.effective_stock,
            note=st.note,
        )
        for st in flagged
    ]


def _run_single_scenario(scenario_id: str, sku: str, scan_id: int) -> WorklistCard:
    """Run one SKU through the graph and build its worklist card.

    Args:
        scenario_id: The owning scenario id (S1/S2/S4).
        sku: The SKU to run.
        scan_id: Scan counter, used to scope the run id.

    Returns:
        A populated :class:`WorklistCard` (auto / draft / suppress).

    Rationale: S1 completes auto-issued, S4 completes suppressed, S2 pauses at the
        human gate — all through the same runner verb, no per-scenario branching.
    """
    run_id = f"s{scan_id}-{sku}"
    outcome = runner.run_for_sku(sku, run_id=run_id, thread_id=run_id)
    values = runner.snapshot(outcome.thread_id)
    return _card_from_state(scenario_id, values, outcome)


def _run_batch_scenario(scenario_id: str, scan_id: int) -> list[WorklistCard]:
    """Run the S3 confectionery batch and build its cards.

    Args:
        scenario_id: The owning scenario id (``'S3'``).
        scan_id: Scan counter (scopes the batch run id).

    Returns:
        One consolidated-PO card per shared vendor, plus the SKU-708 escalation
        draft card.

    Rationale: consolidation is a batch property — the cards make "7 SKUs → 3
        vendor POs + 1 escalation" visible exactly as the scenario intends.
    """
    batch: BatchOutcome = runner.run_batch(list(S3_SKUS), run_id=f"s{scan_id}-batch")
    cards: list[WorklistCard] = []
    for cpo in batch.consolidated_pos:
        cards.append(
            WorklistCard(
                scenario=scenario_id,
                kind="consolidated",
                title=f"Consolidated PO — {cpo.vendor_id}",
                skus=list(cpo.skus),
                route=AutonomyTier.AUTO_ISSUE,
                route_label=route_label(AutonomyTier.AUTO_ISSUE),
                vendor_id=cpo.vendor_id,
                vendor_status="APPROVED",
                po_number=cpo.po_number,
                po_status="ISSUED",
                po_written=True,
                total_cost=cpo.total_cost,
                line_skus=list(cpo.skus),
            )
        )
    # SKU-708 (Vendor-YZ1 SUSPENDED) comes back paused on the draft path.
    for draft in batch.draft_runs:
        values = runner.snapshot(draft.thread_id)
        cards.append(_card_from_state(scenario_id, values, draft))
    return cards


def _card_from_state(
    scenario_id: str, values: dict[str, Any], outcome: RunOutcome
) -> WorklistCard:
    """Flatten a checkpointed graph state + run outcome into a worklist card.

    Args:
        scenario_id: The owning scenario id.
        values: The graph's channel values (from :func:`runner.snapshot`).
        outcome: The :class:`RunOutcome` summarising the run.

    Returns:
        A :class:`WorklistCard` whose ``kind`` follows the route (auto / draft /
        suppress) and whose fields are read straight from the per-agent verdicts.

    Rationale: the single mapping from pipeline state to a render-ready card, so
        every single-SKU and escalation card is built one consistent way.
    """
    stock = values.get("stock")
    forecast = values.get("forecast")
    vendor = values.get("vendor")
    decision = values.get("decision")
    po = values.get("po")
    notif = values.get("notification")
    route = outcome.route

    sku = outcome.sku
    kind = _kind_for_route(route, outcome.paused_for_approval)
    card = WorklistCard(
        scenario=scenario_id,
        kind=kind,
        title=f"{sku} — {stock.description if stock else sku}",
        skus=[sku],
        route=route,
        route_label=route_label(route),
        description=stock.description if stock else None,
        on_hand=stock.on_hand if stock else None,
        reorder_threshold=stock.reorder_threshold if stock else None,
        effective_stock=stock.effective_stock if stock else None,
        signal=stock.signal.value if stock else None,
        forecast_qty=forecast.qty_needed if forecast else None,
        forecast_basis=forecast.forecast_basis if forecast else None,
        weeks_of_cover=forecast.weeks_of_cover if forecast else None,
        vendor_id=vendor.primary_vendor_id if vendor else None,
        vendor_status=vendor.vendor_status if vendor else None,
        approval_status=vendor.approval_status if vendor else None,
        po_number=(po.po_number if po else None) or outcome.po_number,
        po_status=po.status if po else None,
        po_written=bool(po.written) if po else outcome.po_written,
        total_cost=po.total_cost if (po and po.written) else None,
        notification_subject=(notif.subject if notif else None) or outcome.notification_subject,
        run_id=outcome.run_id,
    )

    if kind == "draft":
        _attach_draft_fields(card, decision, outcome, sku)
    return card


def _kind_for_route(route: AutonomyTier | None, paused: bool) -> str:
    """Choose the card layout from the route and pause state.

    Args:
        route: The autonomy tier the run resolved to.
        paused: Whether the run is paused at the human gate.

    Returns:
        ``'draft'`` (paused for approval), ``'suppress'``, or ``'auto'``.

    Rationale: a draft that is still paused renders the approval surface; once
        decided it falls through to the auto/suppress presentation.
    """
    if route is AutonomyTier.DRAFT_FOR_APPROVAL and paused:
        return "draft"
    if route is AutonomyTier.SUPPRESS:
        return "suppress"
    return "auto"


def _attach_draft_fields(
    card: WorklistCard, decision: Any, outcome: RunOutcome, sku: str
) -> None:
    """Populate the approval-surface fields on a draft card, in place.

    Args:
        card: The draft card to enrich.
        decision: The :class:`ApprovalDecision` from the graph state (may be None).
        outcome: The run outcome (carries the interrupt's approval request).
        sku: The SKU (the chart target).

    Rationale: the draft narrative, reason, SLA deadline, and the 90-day chart
        target are exactly what the planner needs to rule on the draft PO.
    """
    request = outcome.approval_request or {}
    card.draft_narrative = (
        getattr(decision, "justification_narrative", None) or request.get("draft_narrative")
    )
    card.draft_reason = getattr(decision, "reason", None) or request.get("reason")
    card.is_critical = (
        getattr(decision, "is_critical", None)
        if decision is not None
        else bool(request.get("is_critical"))
    ) or False
    card.approval_deadline = (
        getattr(decision, "approval_deadline", None) or request.get("approval_deadline")
    )
    card.chart_sku = sku


# ---------------------------------------------------------------------------
# Approve / reject (the human-in-the-loop verbs)
# ---------------------------------------------------------------------------
def approve_card(card: WorklistCard, approver: str, note: str | None) -> WorklistCard:
    """Approve a paused draft card; the PO Generator then writes the PO.

    Args:
        card: The draft card to approve (carries the ``run_id``).
        approver: Handle of the approving planner, recorded on the PO and audit.
        note: Optional approval note.

    Returns:
        A refreshed :class:`WorklistCard` reflecting the written PO.

    Raises:
        ValueError: If the card is not an approvable draft.

    Rationale: the only path a draft tier ever reaches Blue Yonder — the guardrail
        in the PO node permits the write because a human has now approved.
    """
    if card.kind != "draft" or not card.run_id:
        raise ValueError("approve_card called on a non-draft card.")
    outcome = runner.approve(card.run_id, decision=approver, note=note)
    return _refresh_decided_card(card, outcome, "Approved")


def reject_card(card: WorklistCard, reason: str, rejecter: str) -> WorklistCard:
    """Reject a paused draft card; no PO is written and procurement is notified.

    Args:
        card: The draft card to reject (carries the ``run_id``).
        reason: Why the draft is rejected — logged and shown to procurement.
        rejecter: Handle of the rejecting planner.

    Returns:
        A refreshed :class:`WorklistCard`: no PO, alternate-sourcing flagged.

    Raises:
        ValueError: If the card is not an approvable draft.

    Rationale: the human-in-the-loop "no" — Blue Yonder stays untouched, the
        reason hits the immutable audit trail, and procurement is told to source
        elsewhere.
    """
    if card.kind != "draft" or not card.run_id:
        raise ValueError("reject_card called on a non-draft card.")
    outcome = runner.reject(card.run_id, reason=reason, decision=rejecter)
    return _refresh_decided_card(card, outcome, "Rejected")


def _refresh_decided_card(
    card: WorklistCard, outcome: RunOutcome, decision_label: str
) -> WorklistCard:
    """Rebuild a draft card after a human ruling resolved it.

    Args:
        card: The original draft card.
        outcome: The completed run outcome after approve/reject.
        decision_label: ``'Approved'`` or ``'Rejected'``.

    Returns:
        A new card carrying the post-decision PO/notification state and a
        ``decided`` flag, so the UI swaps the buttons for the outcome.

    Rationale: keeps the worklist card object the single source of truth the UI
        re-renders, rather than mutating the paused card in place.
    """
    values = runner.snapshot(outcome.thread_id)
    refreshed = _card_from_state(card.scenario, values, outcome)
    # The run is no longer paused, so _card_from_state classified it auto/suppress;
    # keep the draft narrative/context for the record and mark it decided.
    refreshed.kind = "draft"
    refreshed.decided = True
    refreshed.decision_label = decision_label
    refreshed.draft_narrative = card.draft_narrative
    refreshed.draft_reason = card.draft_reason
    refreshed.is_critical = card.is_critical
    refreshed.approval_deadline = card.approval_deadline
    refreshed.chart_sku = card.chart_sku
    refreshed.title = card.title
    return refreshed


# ---------------------------------------------------------------------------
# LLM toggle (demo control)
# ---------------------------------------------------------------------------
def set_use_llm(enabled: bool) -> None:
    """Flip the LLM master switch for subsequent runs.

    Args:
        enabled: True to try the Gemini→Ollama chain; False for template-only.

    Rationale: lets the demo show the LLM-written narrative *and* prove the
        offline template fallback, without restarting — the agents read
        ``config.USE_LLM`` on every call.
    """
    config.USE_LLM = enabled
    log.info("USE_LLM set to %s for subsequent runs", enabled)
