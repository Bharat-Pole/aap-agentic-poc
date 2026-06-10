"""Human-in-the-loop tests — the Phase-4 acceptance scenarios (S2).

A full S2 draft run, both ways:

* pause -> approve  => exactly one APPROVED PO written;
* pause -> reject   => zero POs, a logged rejection, the alternate-sourcing flag
  raised, and a notice delivered to the (mock) procurement desk.

Also pins the SLA/escalation stub: a CRITICAL-LOW draft surfaces an
``approval_deadline`` on the interrupt payload for the dashboard to display.
"""

from __future__ import annotations

from sqlalchemy import func, select

from agents.notification import PROCUREMENT_ADDRESS
from data.database import get_session
from data.models import BlueYonderPO, Notification
from orchestration import runner


def _po_rows(sku: str) -> int:
    """Count blue_yonder_po lines for a SKU (see test_guardrails)."""
    with get_session() as s:
        return s.scalar(
            select(func.count()).select_from(BlueYonderPO).where(BlueYonderPO.sku == sku)
        )


def test_s2_approve_writes_exactly_one_approved_po(fresh_db):
    """pause -> approve => exactly one PO, status APPROVED, run completed."""
    paused = runner.run_for_sku("SKU-456", run_id="h-appr", thread_id="h-appr")
    assert paused.paused_for_approval is True

    done = runner.approve("h-appr", decision="@dp.approve", note="go ahead")
    assert done.status == "completed"
    assert done.po_written is True and done.po_number
    assert _po_rows("SKU-456") == 1

    with get_session() as s:
        status = s.scalar(select(BlueYonderPO.status).where(BlueYonderPO.sku == "SKU-456"))
    assert status == "APPROVED"


def test_s2_reject_writes_no_po_and_notifies_procurement(fresh_db):
    """pause -> reject => zero POs, alternate-sourcing flag, procurement notice."""
    runner.run_for_sku("SKU-456", run_id="h-rej", thread_id="h-rej")

    done = runner.reject("h-rej", reason="budget freeze this quarter")
    assert done.status == "completed"
    assert done.po_written is False
    assert done.alternate_sourcing is True
    assert _po_rows("SKU-456") == 0

    # A procurement notice was recorded (the mock "notify procurement").
    with get_session() as s:
        notice = s.scalar(
            select(Notification).where(
                Notification.sku == "SKU-456",
                Notification.recipient == PROCUREMENT_ADDRESS,
            )
        )
    assert notice is not None
    assert "alternate sourcing" in notice.body.lower()


def test_critical_low_draft_carries_sla_deadline(fresh_db):
    """A CRITICAL-LOW draft (SKU-456, 30/150) surfaces an approval_deadline."""
    paused = runner.run_for_sku("SKU-456", run_id="h-sla", thread_id="h-sla")
    assert paused.paused_for_approval is True
    assert paused.approval_request is not None
    assert paused.approval_request.get("is_critical") is True
    assert paused.approval_request.get("approval_deadline")
    assert paused.approval_deadline  # also hoisted onto the outcome for the UI.
