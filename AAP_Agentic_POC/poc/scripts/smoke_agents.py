"""Phase-1 smoke test: route the canonical SKUs through the first four agents.

Run from the ``poc/`` source root (after seeding the DB)::

    python scripts/seed_data.py
    python scripts/smoke_agents.py

It pushes SKU-123, SKU-456, and SKU-212 through
StockMonitor -> DemandForecast -> VendorChecker -> Approval and prints the
resolved autonomy tier for each. Expected:

    SKU-123 -> AUTO-ISSUE
    SKU-456 -> DRAFT-FOR-APPROVAL
    SKU-212 -> SUPPRESS

The script asserts those outcomes and exits non-zero if any drifts, so it
doubles as a deterministic regression check for the routing core.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

try:  # UTF-8 stdout so arrows/× render on a cp1252 Windows console.
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

from agents import approval, demand_forecast, stock_monitor, vendor_checker  # noqa: E402
from agents.enums import AutonomyTier  # noqa: E402
from agents.state import new_state  # noqa: E402
from data.database import get_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("smoke_agents")

# SKU -> the tier we expect the deterministic core to resolve.
EXPECTED: dict[str, AutonomyTier] = {
    "SKU-123": AutonomyTier.AUTO_ISSUE,
    "SKU-456": AutonomyTier.DRAFT_FOR_APPROVAL,
    "SKU-212": AutonomyTier.SUPPRESS,
}


def route_sku(sku: str) -> None:
    """Run one SKU through the first four agents and print/assert its route.

    Args:
        sku: The SKU to route.

    Raises:
        AssertionError: If the resolved tier differs from the expectation.

    Rationale: a single shared session per SKU mirrors how the orchestration
        layer will reuse one transaction across nodes.
    """
    state = new_state(sku)
    with get_session() as session:
        stock_monitor.run(state, session)
        demand_forecast.run(state, session)
        vendor_checker.run(state, session)
        approval.run(state, session)

    decision = state.decision
    assert decision is not None, f"{sku}: no decision produced"

    print(f"\n{'=' * 64}\n{sku}: {decision.tier.display}  (confidence {decision.confidence:.2f})")
    print(f"  stock    : {state.stock.signal.value} — {state.stock.note}")
    print(f"  forecast : {state.forecast.forecast_basis}")
    print(f"  vendor   : {state.vendor.note}")
    print(f"  decision : {decision.note}")

    expected = EXPECTED[sku]
    assert decision.tier is expected, (
        f"{sku}: expected {expected.display}, got {decision.tier.display}"
    )


def main() -> int:
    """Route every canonical SKU and report pass/fail.

    Returns:
        Process exit code: 0 if all routes match, 1 otherwise.

    Rationale: exit code lets the smoke test gate CI / a pre-demo check.
    """
    failures: list[str] = []
    for sku in EXPECTED:
        try:
            route_sku(sku)
        except AssertionError as exc:
            failures.append(str(exc))
            log.error("ROUTE MISMATCH: %s", exc)

    print(f"\n{'=' * 64}")
    if failures:
        print(f"FAIL — {len(failures)} mismatch(es):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all routes match expectation "
          "(SKU-123 AUTO-ISSUE, SKU-456 DRAFT-FOR-APPROVAL, SKU-212 SUPPRESS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
