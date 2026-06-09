"""The shared, typed pipeline state every agent reads from and writes to.

One ``ReplenishmentState`` object flows through the six agents. Each agent's
``run(state) -> state`` reads the fields it needs and fills its own slot:

    StockMonitor   -> state.stock
    DemandForecast -> state.forecast
    VendorChecker  -> state.vendor
    Approval       -> state.decision
    POGenerator    -> state.po
    Notification   -> state.notification

The human-in-the-loop fields (``human_approved`` / ``approved_by``) are set
between the Approval and PO-Generator steps on the draft path; in Phase 2 a
LangGraph interrupt populates them from the Streamlit approval surface.

This module imports each agent's *output* model (not the reverse), which is why
the agents only reference ``ReplenishmentState`` under ``TYPE_CHECKING`` — it
keeps the import graph acyclic.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.approval import ApprovalDecision
from agents.demand_forecast import ForecastResult
from agents.notification import NotificationResult
from agents.po_generator import POResult
from agents.stock_monitor import StockStatus
from agents.vendor_checker import VendorAssessment


class ReplenishmentState(BaseModel):
    """The single state object shared across the agent pipeline (one per SKU run).

    Attributes are filled progressively; a ``None`` slot means that agent has
    not run yet. The four canonical scenarios are distinguished entirely by the
    values these slots take, never by branching outside the agents.
    """

    run_id: str = Field(..., description="Correlation id tying this run's audit rows.")
    sku: str = Field(..., description="The SKU this pipeline run is processing.")

    # Human-in-the-loop gate (draft path).
    human_approved: bool | None = Field(
        None, description="True/False once a human rules on a draft; None = pending."
    )
    approved_by: str | None = Field(
        None, description="Handle of the human approver (draft path)."
    )

    # Per-agent output slots.
    stock: StockStatus | None = None
    forecast: ForecastResult | None = None
    vendor: VendorAssessment | None = None
    decision: ApprovalDecision | None = None
    po: POResult | None = None
    notification: NotificationResult | None = None

    # Non-fatal issues collected during the run (for the dashboard).
    errors: list[str] = Field(default_factory=list)


def new_state(sku: str, run_id: str | None = None) -> ReplenishmentState:
    """Construct a fresh state for a SKU run.

    Args:
        sku: The SKU to process.
        run_id: Optional correlation id; defaults to ``run-<sku>``.

    Returns:
        An empty :class:`ReplenishmentState` ready for the first agent.

    Rationale: one constructor keeps run-id conventions consistent across the
        smoke script, the orchestration layer, and the dashboard.
    """
    return ReplenishmentState(run_id=run_id or f"run-{sku}", sku=sku)
