"""The four canonical demo scenarios, as a UI-facing registry.

The dashboard's scenario filter and per-card labelling key off this table. It
maps each scenario id (S1..S4) onto the SKUs it covers and how the orchestration
runner should execute it — a single-SKU graph run, or the S3 *batch* that
consolidates by vendor. Keeping the mapping here (rather than scattered through
``app.py``) means the demo's "which SKU belongs to which scenario" question has
exactly one authoritative answer, and a reviewer can read the whole demo surface
in one place.

The SKU lists mirror the seed (``scripts/seed_data.py``); nothing here re-derives
data — it only names what the seeded scenarios are so the UI can group by them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: The S3 confectionery batch — eight SKUs across four vendors (one suspended).
S3_SKUS: tuple[str, ...] = tuple(f"SKU-{n}" for n in range(701, 709))


class Scenario(BaseModel):
    """One demo scenario the planner can run and filter on.

    Attributes mirror the master spec's four canonical cases so a stakeholder can
    line the dashboard up against the brief one row at a time.
    """

    id: str = Field(..., description="Scenario id, e.g. 'S1'.")
    title: str = Field(..., description="Short human label for the filter/badge.")
    kind: str = Field(..., description="'single' (one graph run) or 'batch' (S3).")
    skus: tuple[str, ...] = Field(..., description="SKUs this scenario covers.")
    expected: str = Field(..., description="The routing outcome the demo expects.")
    summary: str = Field(..., description="One-line description shown above the cards.")


#: The canonical four, in demo order. Order here is the order shown in the UI.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="S1",
        title="Auto-issue",
        kind="single",
        skus=("SKU-123",),
        expected="AUTO-ISSUED",
        summary="SKU-123 Detergent 50/100, Vendor-ABC APPROVED → PO auto-issued and sent.",
    ),
    Scenario(
        id="S2",
        title="Draft for approval",
        kind="single",
        skus=("SKU-456",),
        expected="NEEDS APPROVAL",
        summary="SKU-456 Shampoo 30/150, Vendor-XYZ PENDING (no approved backup) → "
        "draft PO awaits a human.",
    ),
    Scenario(
        id="S3",
        title="Seasonal surge (batch)",
        kind="batch",
        skus=S3_SKUS,
        expected="AUTO-ISSUED ×7 (consolidated) + 1 escalated",
        summary="Confectionery Diwali surge: 7 SKUs auto-issue across 3 vendors with "
        "PO consolidation; SKU-708 (Vendor-YZ1 SUSPENDED) is escalated.",
    ),
    Scenario(
        id="S4",
        title="Suppression",
        kind="single",
        skus=("SKU-212",),
        expected="SUPPRESSED",
        summary="SKU-212 Cooking Oil 80/200, open IN-TRANSIT PO-2025-00772 (1500u, ETA 2d) "
        "already covers it → no new PO.",
    ),
)

#: Reverse index: SKU -> scenario id, for tagging a flagged SKU in the scan list.
SKU_TO_SCENARIO: dict[str, str] = {
    sku: sc.id for sc in SCENARIOS for sku in sc.skus
}


def scenario_for_sku(sku: str) -> str:
    """Return the scenario id a SKU belongs to (or '—' if it is not a scenario SKU).

    Args:
        sku: The SKU to classify.

    Returns:
        The owning scenario id (e.g. ``'S3'``), or ``'—'`` for a background SKU.

    Rationale: the scan can flag any breached SKU; this tags the canonical ones so
        the demo's four cases are easy to pick out of the detection list.
    """
    return SKU_TO_SCENARIO.get(sku, "—")


def get_scenario(scenario_id: str) -> Scenario | None:
    """Look up a scenario by id.

    Args:
        scenario_id: e.g. ``'S2'``.

    Returns:
        The :class:`Scenario`, or ``None`` if the id is unknown.

    Rationale: the service layer resolves a filter selection back to its SKUs.
    """
    for sc in SCENARIOS:
        if sc.id == scenario_id:
            return sc
    return None
