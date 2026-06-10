"""Audit-trail hardening tests (Phase 4).

The audit log is the POC's evidence base, so these assert it behaves like one:

* it is append-only *in practice* — the DB triggers abort any UPDATE or DELETE;
* every agent on a completed run leaves a row under the run_id;
* a suppression writes a dedicated SUPPRESSED event carrying the exact
  effective-stock arithmetic that justified placing no order;
* the human approve / reject rulings are recorded as decision events with the
  approver and their note / reason.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select, text

from data.database import ENGINE, get_session
from data.models import AuditLog
from orchestration import runner

SIX_AGENTS = {
    "StockMonitorAgent",
    "DemandForecastAgent",
    "VendorCheckerAgent",
    "ApprovalAgent",
    "POGenerator",
    "NotificationAgent",
}


def _audit_row(run_id: str, event_type: str) -> AuditLog | None:
    """Fetch the single audit row of an event type for a run.

    Args:
        run_id: The run correlation id.
        event_type: The audit event_type to fetch.

    Returns:
        The matching :class:`AuditLog` row, or ``None``.

    Rationale: most audit assertions inspect one decision event's summary/details.
    """
    with get_session() as s:
        return s.scalar(
            select(AuditLog).where(
                AuditLog.run_id == run_id, AuditLog.event_type == event_type
            )
        )


def test_audit_log_rejects_update(fresh_db):
    """The append-only trigger aborts any UPDATE to audit_log."""
    runner.run_for_sku("SKU-123", run_id="au-upd", thread_id="au-upd")
    with pytest.raises(Exception):
        with ENGINE.begin() as conn:
            conn.execute(
                text("UPDATE audit_log SET summary='tampered' WHERE run_id='au-upd'")
            )


def test_audit_log_rejects_delete(fresh_db):
    """The append-only trigger aborts any DELETE from audit_log."""
    runner.run_for_sku("SKU-123", run_id="au-del", thread_id="au-del")
    with pytest.raises(Exception):
        with ENGINE.begin() as conn:
            conn.execute(text("DELETE FROM audit_log WHERE run_id='au-del'"))
    # And the rows are still there.
    with get_session() as s:
        n = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.run_id == "au-del")
        )
    assert n > 0


def test_every_agent_writes_an_audit_row(fresh_db):
    """A completed auto-issue run (S1) leaves a row for all six agents."""
    runner.run_for_sku("SKU-123", run_id="au-six", thread_id="au-six")
    with get_session() as s:
        agents = set(
            s.scalars(
                select(AuditLog.agent).where(AuditLog.run_id == "au-six").distinct()
            ).all()
        )
    assert SIX_AGENTS.issubset(agents)


def test_suppressed_event_carries_effective_stock_calc(fresh_db):
    """The SUPPRESSED event records the effective-stock arithmetic (S4)."""
    runner.run_for_sku("SKU-212", run_id="au-sup", thread_id="au-sup")
    row = _audit_row("au-sup", "SUPPRESSED")
    assert row is not None
    calc = json.loads(row.details)["effective_stock_calc"]
    assert calc["effective_stock"] == calc["on_hand"] + calc["in_transit_qty"]
    assert calc["covers_threshold"] is True
    assert calc["in_transit_refs"]  # the inbound PO that covers the breach.


def test_approval_decision_event_records_note(fresh_db):
    """Approving a draft writes an APPROVAL_GRANTED event with approver + note."""
    runner.run_for_sku("SKU-456", run_id="au-appr", thread_id="au-appr")
    runner.approve("au-appr", decision="@mgr.jones", note="signed off, urgent")
    row = _audit_row("au-appr", "APPROVAL_GRANTED")
    assert row is not None
    blob = row.summary + (row.details or "")
    assert "@mgr.jones" in blob
    assert "signed off, urgent" in blob


def test_rejection_decision_event_records_reason(fresh_db):
    """Rejecting a draft writes an APPROVAL_REJECTED event with the reason."""
    runner.run_for_sku("SKU-456", run_id="au-rej", thread_id="au-rej")
    runner.reject("au-rej", reason="duplicate of an existing order")
    row = _audit_row("au-rej", "APPROVAL_REJECTED")
    assert row is not None
    assert "duplicate of an existing order" in (row.summary + (row.details or ""))
