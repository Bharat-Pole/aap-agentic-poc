"""Phase-1 acceptance gate — proves the deterministic agent core is complete.

Run from the ``poc/`` source root::

    python scripts/seed_data.py
    python scripts/validate_phase1.py

It exercises every Phase-1 deliverable against the seeded scenarios and prints a
PASS/FAIL checklist, exiting non-zero if anything regresses:

* S1 SKU-123 routes AUTO-ISSUE and writes one ISSUED PO line.
* S2 SKU-456 routes DRAFT — no write until a human approves, then APPROVED.
* S3 SKU-701..708 — 7 auto-issue across 3 vendors consolidate to 3 POs;
  SKU-708 (Vendor-YZ1 SUSPENDED) escalates to DRAFT and is not consolidated.
* S4 SKU-212 routes SUPPRESS — never writes a PO (the guardrail).
* Every agent appends to audit_log; blue_yonder_po has no suppressed SKU.
* audit_log is immutable (UPDATE is rejected by the DB trigger).

This is the deterministic check to run before declaring Phase 1 done or demoing.
"""

from __future__ import annotations

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

from sqlalchemy import func, select, text  # noqa: E402

from agents import (  # noqa: E402
    approval,
    demand_forecast,
    notification,
    po_generator,
    stock_monitor,
    vendor_checker,
)
from agents.enums import AutonomyTier  # noqa: E402
from agents.mock_blue_yonder import POLineSpec  # noqa: E402
from agents.state import new_state  # noqa: E402
from data.database import get_session  # noqa: E402
from data.models import AuditLog, BlueYonderPO, Notification  # noqa: E402

FOUR_AGENTS = (stock_monitor, demand_forecast, vendor_checker, approval)
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one named assertion result for the final checklist.

    Args:
        name: Human-readable check label.
        ok: Whether the check passed.
        detail: Optional extra context shown after the label.

    Rationale: collect-then-report so one failure doesn't hide the rest.
    """
    results.append((ok, f"{name}{(' — ' + detail) if detail else ''}"))


def _route(sku: str, session) -> "object":
    """Run a SKU through the first four agents and return the state.

    Args:
        sku: The SKU to route.
        session: Shared ORM session.

    Returns:
        The populated ReplenishmentState.

    Rationale: the routing core shared by every scenario check.
    """
    state = new_state(sku, run_id=f"validate-{sku}")
    for ag in FOUR_AGENTS:
        ag.run(state, session)
    return state


def validate_s1(session) -> None:
    """S1: SKU-123 auto-issues and writes exactly one ISSUED PO line."""
    st = _route("SKU-123", session)
    check("S1 SKU-123 routes AUTO-ISSUE", st.decision.tier is AutonomyTier.AUTO_ISSUE,
          st.decision.tier.display)
    po_generator.run(st, session)
    session.commit()
    check("S1 PO written, status ISSUED",
          st.po.written and st.po.status == "ISSUED" and len(st.po.lines) == 1,
          f"PO {st.po.po_number}, qty {st.forecast.qty_needed}")


def validate_s2(session) -> None:
    """S2: SKU-456 drafts; no write until approved, then status APPROVED."""
    st = _route("SKU-456", session)
    check("S2 SKU-456 routes DRAFT-FOR-APPROVAL",
          st.decision.tier is AutonomyTier.DRAFT_FOR_APPROVAL, st.decision.reason)
    check("S2 draft requires human approval", st.decision.requires_human is True)

    po_generator.run(st, session)  # no approval yet
    check("S2 no PO written before approval (guardrail)",
          st.po.written is False, st.po.skipped_reason or "")

    st.human_approved, st.approved_by, st.po = True, "@dp.smoke", None
    po_generator.run(st, session)
    session.commit()
    check("S2 PO written APPROVED after human approval",
          st.po.written and st.po.status == "APPROVED", st.po.po_number or "")


def validate_s3(session) -> None:
    """S3: 7 auto-issue across 3 vendors -> 3 consolidated POs; 708 escalates."""
    states = [_route(f"SKU-{n}", session) for n in range(701, 709)]
    auto = [s for s in states if s.decision.tier is AutonomyTier.AUTO_ISSUE]
    draft = [s for s in states if s.decision.tier is AutonomyTier.DRAFT_FOR_APPROVAL]
    check("S3 seven SKUs auto-issue", len(auto) == 7, f"{len(auto)}/8 auto")
    check("S3 SKU-708 escalates to DRAFT (Vendor-YZ1 SUSPENDED)",
          len(draft) == 1 and draft[0].sku == "SKU-708",
          draft[0].decision.reason if draft else "none")

    groups = vendor_checker.consolidate_by_vendor([s.vendor for s in states])
    check("S3 consolidates to 3 vendor PO groups", len(groups) == 3,
          ", ".join(f"{v}:{len(a)}" for v, a in groups.items()))
    check("S3 suspended vendor excluded from consolidation",
          "Vendor-YZ1" not in groups)

    pos = []
    for vendor_id, assessments in groups.items():
        lines = [
            POLineSpec(
                sku=a.sku,
                quantity=next(s for s in states if s.sku == a.sku).forecast.qty_needed,
                unit_cost=a.unit_cost or 0.0,
            )
            for a in assessments
        ]
        pos.append(po_generator.generate_consolidated(
            session, run_id="validate-S3", vendor_id=vendor_id, lines=lines))
    session.commit()
    check("S3 three consolidated POs written, 7 lines total",
          len(pos) == 3 and sum(len(p.lines) for p in pos) == 7,
          ", ".join(f"{p.po_number}:{len(p.lines)}L" for p in pos))


def validate_s4(session) -> None:
    """S4: SKU-212 suppresses and never writes a PO."""
    st = _route("SKU-212", session)
    check("S4 SKU-212 routes SUPPRESS", st.decision.tier is AutonomyTier.SUPPRESS)
    po_generator.run(st, session)
    notification.run(st, session)
    session.commit()
    check("S4 SUPPRESS writes no PO (guardrail)",
          st.po.written is False, st.po.skipped_reason or "")
    check("S4 advisory notification recorded",
          st.notification is not None and st.notification.urgency.value == "low")


def validate_crosscutting(session) -> None:
    """Audit completeness, the never-suppress invariant, and immutability."""
    agents_logged = set(
        session.scalars(select(AuditLog.agent).distinct()).all()
    )
    expected_agents = {
        "StockMonitorAgent", "DemandForecastAgent", "VendorCheckerAgent",
        "ApprovalAgent", "POGenerator", "NotificationAgent",
    }
    check("Every agent appended to audit_log",
          expected_agents.issubset(agents_logged),
          f"{len(agents_logged & expected_agents)}/6 agents")

    by_pin = session.scalar(
        select(func.count()).select_from(BlueYonderPO).where(BlueYonderPO.sku == "SKU-212")
    )
    check("No PO line ever written for the suppressed SKU-212", by_pin == 0,
          f"{by_pin} rows")

    notif_212 = session.scalar(
        select(func.count()).select_from(Notification).where(Notification.sku == "SKU-212")
    )
    check("Suppression still notified the DP (advisory)", notif_212 >= 1)

    # audit_log must reject mutation (DB trigger).
    try:
        session.execute(text("UPDATE audit_log SET summary='tampered' WHERE id=1"))
        session.commit()
        immutable = False
    except Exception:
        session.rollback()
        immutable = True
    check("audit_log is immutable (UPDATE rejected by trigger)", immutable)


def main() -> int:
    """Run every validation and print the PASS/FAIL checklist.

    Returns:
        Exit code 0 if all checks pass, 1 otherwise.

    Rationale: a single deterministic gate for 'is Phase 1 done?'.
    """
    with get_session() as session:
        validate_s1(session)
        validate_s2(session)
        validate_s3(session)
        validate_s4(session)
        validate_crosscutting(session)

    print("\nPhase 1 acceptance checklist\n" + "=" * 60)
    for ok, label in results:
        print(f"  {'✓' if ok else '✗'} {label}")

    failed = [l for ok, l in results if not ok]
    print("=" * 60)
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} check(s) failed.")
        return 1
    print(f"PASS — all {len(results)} checks passed. Phase 1 is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
