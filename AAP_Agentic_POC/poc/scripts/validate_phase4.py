"""Phase-4 acceptance gate — proves the guardrails, HITL, and audit hardening.

Run from the ``poc/`` source root (re-seeds the DB itself for determinism)::

    python scripts/validate_phase4.py

It drives the human-in-the-loop and guardrail requirements through the compiled
graph and the ``approve`` / ``reject`` orchestration verbs, printing a PASS/FAIL
checklist and exiting non-zero if anything regresses:

* **S2 approve** — ``run_for_sku('SKU-456')`` pauses; ``approve(run_id)`` writes
  **exactly one** PO with status APPROVED and logs ``APPROVAL_GRANTED`` (with note).
* **S2 reject** — a fresh draft, ``reject(run_id, reason)`` writes **zero** POs,
  raises the alternate-sourcing flag, logs ``APPROVAL_REJECTED`` (with reason), and
  records a notice to the (mock) procurement desk.
* **Guard** — a SUPPRESS run (S4) never creates a ``blue_yonder_po`` row; a paused
  draft has no PO until approved.
* **Audit** — ``audit_log`` rejects UPDATE/DELETE; the ``SUPPRESSED`` event carries
  the effective-stock calculation.
* **SLA** — a CRITICAL-LOW draft surfaces an ``approval_deadline`` on the interrupt.

The pytest suites under ``tests/`` assert the same invariants in finer grain; this
script is the single deterministic gate to run before declaring Phase 4 done.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

try:  # UTF-8 stdout so ✓/✗ render on a cp1252 Windows console.
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

import config  # noqa: E402

config.USE_LLM = False  # deterministic template path; no network during the gate.
logging.disable(logging.CRITICAL)  # keep the checklist clean; agents log at INFO.

from sqlalchemy import func, select, text  # noqa: E402

from agents.notification import PROCUREMENT_ADDRESS  # noqa: E402
from data.database import ENGINE, get_session  # noqa: E402
from data.models import AuditLog, BlueYonderPO, Notification  # noqa: E402
from orchestration import runner  # noqa: E402
from scripts.seed_data import seed  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one named assertion for the final checklist.

    Args:
        name: Human-readable check label.
        ok: Whether the check passed.
        detail: Optional extra context shown after the label.

    Rationale: collect-then-report so one failure doesn't hide the rest.
    """
    results.append((ok, f"{name}{(' — ' + detail) if detail else ''}"))


def _po_lines_for_sku(sku: str) -> int:
    """Count blue_yonder_po lines written for a SKU.

    Args:
        sku: The SKU to count.

    Returns:
        The number of ``blue_yonder_po`` rows referencing the SKU.

    Rationale: the guardrail invariant is checked at the table, not the API.
    """
    with get_session() as s:
        return s.scalar(
            select(func.count()).select_from(BlueYonderPO).where(BlueYonderPO.sku == sku)
        )


def _audit_row(run_id: str, event_type: str) -> AuditLog | None:
    """Fetch one audit row of an event type for a run (or None)."""
    with get_session() as s:
        return s.scalar(
            select(AuditLog).where(
                AuditLog.run_id == run_id, AuditLog.event_type == event_type
            )
        )


def validate_approve() -> None:
    """S2 approve: pause -> approve -> exactly one APPROVED PO + logged grant."""
    paused = runner.run_for_sku("SKU-456", run_id="p4-appr", thread_id="p4-appr")
    check("Approve: SKU-456 pauses at the human gate", paused.paused_for_approval)
    check("Approve: no PO while paused", _po_lines_for_sku("SKU-456") == 0)

    done = runner.approve("p4-appr", decision="@dp.approve", note="ok to proceed")
    check("Approve: run completes", done.status == "completed", done.status)
    check("Approve: exactly one PO written", _po_lines_for_sku("SKU-456") == 1,
          done.po_number or "")
    with get_session() as s:
        status = s.scalar(select(BlueYonderPO.status).where(BlueYonderPO.sku == "SKU-456"))
    check("Approve: PO status is APPROVED", status == "APPROVED", status or "")
    grant = _audit_row("p4-appr", "APPROVAL_GRANTED")
    check("Approve: APPROVAL_GRANTED logged with approver + note",
          grant is not None and "@dp.approve" in (grant.summary + (grant.details or ""))
          and "ok to proceed" in (grant.summary + (grant.details or "")))


def validate_reject() -> None:
    """S2 reject: pause -> reject -> zero POs + logged rejection + procurement notice."""
    runner.run_for_sku("SKU-456", run_id="p4-rej", thread_id="p4-rej")
    before = _po_lines_for_sku("SKU-456")
    done = runner.reject("p4-rej", reason="budget freeze this quarter")
    check("Reject: run completes", done.status == "completed", done.status)
    check("Reject: no new PO written", _po_lines_for_sku("SKU-456") == before)
    check("Reject: alternate-sourcing flag raised", done.alternate_sourcing is True)
    rej = _audit_row("p4-rej", "APPROVAL_REJECTED")
    check("Reject: APPROVAL_REJECTED logged with the reason",
          rej is not None and "budget freeze this quarter" in (rej.summary + (rej.details or "")))
    with get_session() as s:
        notice = s.scalar(
            select(Notification).where(
                Notification.sku == "SKU-456",
                Notification.recipient == PROCUREMENT_ADDRESS,
            )
        )
    check("Reject: procurement notice recorded",
          notice is not None and "alternate sourcing" in (notice.body.lower() if notice else ""))


def validate_guard() -> None:
    """Guard: SUPPRESS never writes a PO; the write guard holds at the table."""
    o = runner.run_for_sku("SKU-212", run_id="p4-s4", thread_id="p4-s4")
    check("Guard: SUPPRESS run writes no PO (S4)",
          not o.po_written and _po_lines_for_sku("SKU-212") == 0, o.route.display if o.route else "")


def validate_audit() -> None:
    """Audit: append-only triggers fire; SUPPRESSED carries the effective-stock calc."""
    runner.run_for_sku("SKU-123", run_id="p4-au", thread_id="p4-au")
    update_blocked = False
    try:
        with ENGINE.begin() as conn:
            conn.execute(text("UPDATE audit_log SET summary='x' WHERE run_id='p4-au'"))
    except Exception:
        update_blocked = True
    check("Audit: UPDATE on audit_log is blocked (append-only)", update_blocked)

    delete_blocked = False
    try:
        with ENGINE.begin() as conn:
            conn.execute(text("DELETE FROM audit_log WHERE run_id='p4-au'"))
    except Exception:
        delete_blocked = True
    check("Audit: DELETE on audit_log is blocked (append-only)", delete_blocked)

    sup = _audit_row("p4-s4", "SUPPRESSED")
    calc = json.loads(sup.details)["effective_stock_calc"] if sup and sup.details else {}
    check("Audit: SUPPRESSED event carries the effective-stock calc",
          bool(calc) and calc.get("effective_stock") == calc.get("on_hand", 0) + calc.get("in_transit_qty", 0)
          and calc.get("covers_threshold") is True)


def validate_sla() -> None:
    """SLA: a CRITICAL-LOW draft surfaces an approval_deadline on the interrupt."""
    o = runner.run_for_sku("SKU-456", run_id="p4-sla", thread_id="p4-sla")
    req = o.approval_request or {}
    check("SLA: CRITICAL-LOW draft is flagged critical", req.get("is_critical") is True)
    check("SLA: an approval_deadline is set and surfaced",
          bool(req.get("approval_deadline")) and bool(o.approval_deadline),
          o.approval_deadline or "")


def main() -> int:
    """Re-seed, run every Phase-4 validation, and print the checklist.

    Returns:
        Exit code 0 if all checks pass, 1 otherwise.

    Rationale: a single deterministic gate for 'is Phase 4 done?'.
    """
    seed()  # clean slate so PO numbering and routing reproduce exactly.
    validate_approve()
    validate_reject()
    validate_guard()
    validate_audit()
    validate_sla()

    print("\nPhase 4 acceptance checklist\n" + "=" * 60)
    for ok, label in results:
        print(f"  {'✓' if ok else '✗'} {label}")

    failed = [l for ok, l in results if not ok]
    print("=" * 60)
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} check(s) failed.")
        return 1
    print(f"PASS — all {len(results)} checks passed. Phase 4 is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
