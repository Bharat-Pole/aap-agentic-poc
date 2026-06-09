"""Print a human-readable summary of poc.db so the seed can be eyeballed.

Run from the ``poc/`` source root::

    python scripts/show_db.py

Shows row counts for every table and then the scenario anchor rows (SKU-123,
SKU-456, SKU-701..708, SKU-212) with the exact values from the spec, plus the
relevant vendor statuses and the in-transit open PO behind the suppression case.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import func, select

# Force UTF-8 stdout so the summary renders on Windows' cp1252 console too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
    pass

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

from data.database import get_session  # noqa: E402
from data.models import (  # noqa: E402
    AuditLog,
    BlueYonderPO,
    Inventory,
    Notification,
    OpenPO,
    PromoCalendar,
    SalesOfftake,
    SeasonIndex,
    UserDirectory,
    VendorApproval,
    VendorMaster,
)

# Tables in a stable display order alongside their model class.
_TABLES = [
    ("inventory", Inventory),
    ("sales_offtake", SalesOfftake),
    ("vendor_master", VendorMaster),
    ("vendor_approval", VendorApproval),
    ("promo_calendar", PromoCalendar),
    ("season_index", SeasonIndex),
    ("open_po", OpenPO),
    ("blue_yonder_po", BlueYonderPO),
    ("notifications", Notification),
    ("audit_log", AuditLog),
    ("user_directory", UserDirectory),
]

# Scenario anchor SKUs in display order.
_ANCHOR_SKUS = ["SKU-123", "SKU-456"] + [f"SKU-{n}" for n in range(701, 709)] + ["SKU-212"]


def _hr(title: str) -> None:
    """Print a section header.

    Args:
        title: Section title to underline.

    Rationale: keeps the console dump scannable.
    """
    print(f"\n{title}\n{'=' * len(title)}")


def _print_counts(session) -> None:
    """Print row counts for every table.

    Args:
        session: Open ORM session.

    Rationale: the first thing to check is that nothing seeded empty.
    """
    _hr("Table row counts")
    for name, model in _TABLES:
        count = session.scalar(select(func.count()).select_from(model))
        print(f"  {name:<18} {count:>6}")


def _weekly_avg(session, sku: str) -> float:
    """Compute the average weekly offtake for a SKU from its daily history.

    Args:
        session: Open ORM session.
        sku: SKU to summarise.

    Returns:
        Mean weekly units (daily mean * 7), or 0.0 if no history.

    Rationale: lets us confirm the seeded ~608/420/310 weekly targets landed.
    """
    rows = session.scalars(
        select(SalesOfftake.units_sold).where(SalesOfftake.sku == sku)
    ).all()
    if not rows:
        return 0.0
    return round(sum(rows) / len(rows) * 7, 1)


def _print_anchor(session, sku: str) -> None:
    """Print the inventory + vendor + demand context for one anchor SKU.

    Args:
        session: Open ORM session.
        sku: Anchor SKU to display.

    Rationale: surfaces the exact fields a reviewer needs to confirm each
    scenario will route as intended.
    """
    inv = session.get(Inventory, sku)
    if inv is None:
        print(f"  {sku}: MISSING")
        return

    vendor = session.get(VendorMaster, inv.primary_vendor_id) if inv.primary_vendor_id else None
    approval = session.scalar(
        select(VendorApproval).where(
            VendorApproval.sku == sku, VendorApproval.is_primary.is_(True)
        )
    )
    promo = session.scalar(select(PromoCalendar).where(PromoCalendar.sku == sku))
    season = session.scalar(select(SeasonIndex).where(SeasonIndex.category == inv.category))

    vendor_str = (
        f"{vendor.vendor_id} [{vendor.status}]" if vendor else "-"
    )
    appr_str = (
        f"{approval.approval_status} MOQ={approval.moq}" if approval else "no approval row"
    )
    promo_str = f"+{promo.uplift_pct:.0%} ({promo.promo_name})" if promo else "none"
    season_str = f"+{season.uplift_pct:.0%}" if season else "-"

    print(
        f"  {sku:<8} {inv.description:<26} {inv.category:<14} "
        f"on_hand={inv.on_hand:>4}/thr={inv.reorder_threshold:<4} pack={inv.pack_size:<3} "
        f"lead={inv.lead_time_days}d x{inv.lead_multiplier:g} peak={inv.peak_season}"
    )
    print(
        f"           vendor={vendor_str:<22} approval={appr_str:<22} "
        f"promo={promo_str:<26} season={season_str}  weekly_offtake~{_weekly_avg(session, sku)}"
    )


def _print_open_pos(session) -> None:
    """Print open / in-transit POs (the S4 suppression signal).

    Args:
        session: Open ORM session.

    Rationale: PO-2025-00772 is what makes SKU-212 suppress; show it explicitly.
    """
    _hr("Open POs (in-transit supply)")
    pos = session.scalars(select(OpenPO)).all()
    if not pos:
        print("  (none)")
    for po in pos:
        print(
            f"  {po.po_number}  {po.sku}  {po.vendor_id}  qty={po.quantity}  "
            f"status={po.status}  eta={po.eta_days}d"
        )


def _print_dps(session) -> None:
    """Print the Demand Planner directory (one per sub-category).

    Args:
        session: Open ORM session.

    Rationale: confirms notification targeting will resolve for every category.
    """
    _hr("Demand Planners (user_directory)")
    for u in session.scalars(select(UserDirectory).order_by(UserDirectory.subcategory)).all():
        print(f"  {u.subcategory:<14} {u.name:<22} {u.slack_handle:<22} {u.email}")


def main() -> None:
    """Render the full seed summary to stdout.

    Rationale: single command to validate a fresh seed at a glance.
    """
    with get_session() as session:
        if session.scalar(select(func.count()).select_from(Inventory)) == 0:
            print("poc.db looks empty — run `python scripts/seed_data.py` first.")
            return

        _print_counts(session)

        _hr("Scenario anchors")
        for sku in _ANCHOR_SKUS:
            _print_anchor(session, sku)

        _print_open_pos(session)
        _print_dps(session)
        print()


if __name__ == "__main__":
    main()
