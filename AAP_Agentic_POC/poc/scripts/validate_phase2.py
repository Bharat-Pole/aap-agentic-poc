"""Phase-2 acceptance gate — proves the LangGraph orchestration is complete.

Run from the ``poc/`` source root (re-seeds the DB itself for determinism)::

    python scripts/validate_phase2.py

It drives the four canonical scenarios *through the compiled graph* and prints a
PASS/FAIL checklist, exiting non-zero if anything regresses:

* S1 ``run_for_sku('SKU-123')`` completes AUTO-ISSUE end to end and writes a PO.
* S2 ``run_for_sku('SKU-456')`` pauses at the human-approval interrupt with an
  ApprovalRequest, writes no PO while paused, then writes an APPROVED PO on
  resume — and writes nothing if the resume rejects.
* S3 ``run_batch('SKU-701'..'SKU-708')`` yields 3 consolidated POs (7 lines)
  across 3 vendors plus 1 paused draft (SKU-708, Vendor-YZ1 SUSPENDED).
* S4 ``run_for_sku('SKU-212')`` suppresses — never writes a PO (the guardrail).
* Every completed run leaves a full six-agent audit trail under its run_id.

This is the deterministic check to run before declaring Phase 2 done or demoing.
"""

from __future__ import annotations

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

logging.disable(logging.CRITICAL)  # keep the checklist clean; agents log at INFO.

from sqlalchemy import func, select  # noqa: E402

from data.database import get_session  # noqa: E402
from data.models import AuditLog, BlueYonderPO  # noqa: E402
from orchestration import runner  # noqa: E402
from orchestration.state import GraphState  # noqa: E402  (kept importable for the demo)

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
    """Count PO lines written to Blue Yonder for a SKU.

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


def _agents_in_audit(run_id: str) -> set[str]:
    """Return the distinct agent names that logged under a run_id.

    Args:
        run_id: The run correlation id.

    Returns:
        The set of ``audit_log.agent`` values for that run.

    Rationale: proves the audit trail is wired through every graph node.
    """
    with get_session() as s:
        return set(
            s.scalars(select(AuditLog.agent).where(AuditLog.run_id == run_id).distinct()).all()
        )


def validate_s1() -> None:
    """S1: run_for_sku('SKU-123') completes auto-issue end to end."""
    o = runner.run_for_sku("SKU-123", run_id="p2-s1", thread_id="p2-s1")
    check("S1 SKU-123 completes (not paused)", o.status == "completed", o.route.display if o.route else "")
    check("S1 auto-issue PO written end to end", o.po_written and bool(o.po_number), o.po_number or "")
    agents = _agents_in_audit("p2-s1")
    expected = {"StockMonitorAgent", "DemandForecastAgent", "VendorCheckerAgent",
                "ApprovalAgent", "POGenerator", "NotificationAgent"}
    check("S1 full six-agent audit trail under run_id", expected.issubset(agents),
          f"{len(agents & expected)}/6")


def validate_s2() -> None:
    """S2: SKU-456 pauses at the interrupt; resume gates the PO write."""
    # Reject path first (own thread) — must write nothing.
    rej = runner.run_for_sku("SKU-456", run_id="p2-s2r", thread_id="p2-s2r")
    check("S2 SKU-456 pauses at human-approval interrupt", rej.paused_for_approval, rej.status)
    check("S2 interrupt surfaces an ApprovalRequest payload",
          isinstance(rej.approval_request, dict) and rej.approval_request.get("sku") == "SKU-456",
          (rej.approval_request or {}).get("reason", ""))
    check("S2 no PO written while paused", not rej.po_written and _po_lines_for_sku("SKU-456") == 0)
    rejected = runner.resume_run("p2-s2r", approved=False, approved_by="@dp.reject")
    check("S2 rejection writes no PO (guardrail)",
          rejected.status == "completed" and not rejected.po_written and _po_lines_for_sku("SKU-456") == 0)

    # Approve path (fresh thread) — must write exactly one APPROVED PO.
    appr = runner.run_for_sku("SKU-456", run_id="p2-s2a", thread_id="p2-s2a")
    check("S2 second run also pauses (deterministic)", appr.paused_for_approval)
    done = runner.resume_run("p2-s2a", approved=True, approved_by="@dp.approve")
    check("S2 approval writes one APPROVED PO on resume",
          done.po_written and _po_lines_for_sku("SKU-456") == 1, done.po_number or "")
    with get_session() as s:
        status = s.scalar(select(BlueYonderPO.status).where(BlueYonderPO.sku == "SKU-456"))
    check("S2 resumed PO carries status APPROVED", status == "APPROVED", status or "")


def validate_s3() -> None:
    """S3: batch yields 3 consolidated POs (7 lines) + 1 paused draft."""
    b = runner.run_batch([f"SKU-{n}" for n in range(701, 709)], run_id="p2-s3")
    check("S3 seven SKUs auto-issue", len(b.auto_issue_skus) == 7, f"{len(b.auto_issue_skus)}/8")
    check("S3 three consolidated POs across three vendors",
          len(b.consolidated_pos) == 3,
          ", ".join(f"{p.vendor_id}:{p.line_count}L" for p in b.consolidated_pos))
    check("S3 consolidated POs cover 7 lines total",
          sum(p.line_count for p in b.consolidated_pos) == 7)
    check("S3 suspended-vendor SKU-708 escalated to a paused draft",
          len(b.draft_runs) == 1 and b.draft_runs[0].sku == "SKU-708"
          and b.draft_runs[0].paused_for_approval)
    check("S3 SKU-708 wrote no PO (paused, awaiting approval)",
          _po_lines_for_sku("SKU-708") == 0)


def validate_s4() -> None:
    """S4: SKU-212 suppresses and never writes a PO."""
    o = runner.run_for_sku("SKU-212", run_id="p2-s4", thread_id="p2-s4")
    check("S4 SKU-212 completes as SUPPRESS", o.status == "completed" and o.route.display == "SUPPRESS",
          o.route.display if o.route else "")
    check("S4 suppression skips the PO node (no PO ever)",
          not o.po_written and _po_lines_for_sku("SKU-212") == 0)
    check("S4 suppression still notified the DP", bool(o.notification_subject))


def main() -> int:
    """Re-seed, run every Phase-2 validation, and print the checklist.

    Returns:
        Exit code 0 if all checks pass, 1 otherwise.

    Rationale: a single deterministic gate for 'is Phase 2 done?'.
    """
    seed()  # clean slate so PO numbering and routing reproduce exactly.
    validate_s1()
    validate_s2()
    validate_s3()
    validate_s4()

    print("\nPhase 2 acceptance checklist\n" + "=" * 60)
    for ok, label in results:
        print(f"  {'✓' if ok else '✗'} {label}")

    failed = [l for ok, l in results if not ok]
    print("=" * 60)
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} check(s) failed.")
        return 1
    print(f"PASS — all {len(results)} checks passed. Phase 2 is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
