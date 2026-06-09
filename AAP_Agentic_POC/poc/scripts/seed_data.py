"""Build poc.db and seed it so all four canonical scenarios trigger.

Run from the ``poc/`` source root::

    python scripts/seed_data.py

The script is fully deterministic (fixed numpy + Faker seed and a fixed
reference "today"), so the seed — and therefore the demo — reproduces byte for
byte on every machine. It drops and recreates the schema each run.

Scenario anchors produced here:

* S1 auto-issue   — SKU-123 Detergent, 50/100, Vendor-ABC APPROVED.
* S2 draft        — SKU-456 Shampoo, 30/150, Vendor-XYZ PENDING (no approved backup).
* S3 batch surge  — SKU-701..708 Confectionery (Diwali); YZ1/708 SUSPENDED.
* S4 suppression  — SKU-212 Cooking Oil, 80/200, open IN-TRANSIT PO-2025-00772.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from faker import Faker

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

from config import RANDOM_SEED, REFERENCE_DATE, SUBCATEGORIES  # noqa: E402
from data.database import get_session, reset_db  # noqa: E402
from data.models import (  # noqa: E402
    Inventory,
    OpenPO,
    PromoCalendar,
    SalesOfftake,
    SeasonIndex,
    UserDirectory,
    VendorApproval,
    VendorMaster,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("seed_data")

OFFTAKE_DAYS = 90  # 90-day daily history feeds the statsmodels forecaster.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _daily_series(weekly_avg: float, n_days: int, rng: np.random.Generator) -> list[int]:
    """Generate a noisy daily unit-sales series with a given weekly mean.

    Args:
        weekly_avg: Target average units sold per week.
        n_days: Number of daily points to generate.
        rng: Seeded numpy generator (keeps output reproducible).

    Returns:
        A list of non-negative integer daily sales.

    Rationale: realistic offtake with mild dispersion so the forecaster has
    something to fit, while the weekly mean stays close to the scenario spec.
    """
    base = weekly_avg / 7.0
    raw = rng.normal(loc=base, scale=max(base * 0.12, 1.0), size=n_days)
    return [int(v) for v in np.clip(np.round(raw), 0, None)]


def _offtake_rows(sku: str, weekly_avg: float, rng: np.random.Generator) -> list[SalesOfftake]:
    """Build 90 days of SalesOfftake rows ending the day before REFERENCE_DATE.

    Args:
        sku: SKU the history belongs to.
        weekly_avg: Target weekly demand used to shape the series.
        rng: Seeded numpy generator.

    Returns:
        A list of SalesOfftake ORM objects.

    Rationale: anchoring to REFERENCE_DATE keeps forecast horizons deterministic.
    """
    series = _daily_series(weekly_avg, OFFTAKE_DAYS, rng)
    start = REFERENCE_DATE - timedelta(days=OFFTAKE_DAYS)
    return [
        SalesOfftake(sku=sku, sale_date=start + timedelta(days=i), units_sold=units)
        for i, units in enumerate(series)
    ]


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
def build_vendors() -> list[VendorMaster]:
    """Construct every supplier referenced by the scenarios plus a healthy pool.

    Returns:
        VendorMaster rows. Statuses encode the routing gates:
        Vendor-XYZ is PENDING (S2) and Vendor-YZ1 is SUSPENDED (S3/SKU-708).

    Rationale: vendor-level status is the coarse gate the Approval Agent honours
    before it even looks at the per-SKU approval registry.
    """
    named = [
        ("Vendor-ABC", "ABC Distributors", "APPROVED", 7),
        ("Vendor-XYZ", "XYZ Supplies", "PENDING", 10),
        ("Vendor-LMN", "LMN Trading", "APPROVED", 8),  # approved backup, no SKU-456
        ("Vendor-MNO", "MNO Wholesale", "APPROVED", 14),
        ("Vendor-PQR", "PQR Confections", "APPROVED", 5),
        ("Vendor-STU", "STU Sweets", "APPROVED", 5),
        ("Vendor-VWX", "VWX Candy Co", "APPROVED", 5),
        ("Vendor-YZ1", "YZ1 Treats", "SUSPENDED", 6),
    ]
    vendors = [
        VendorMaster(
            vendor_id=vid,
            vendor_name=name,
            status=status,
            contact_email=f"orders@{vid.split('-')[1].lower()}.example.com",
            reliability_score=0.95 if status == "APPROVED" else 0.6,
            default_lead_time_days=lead,
        )
        for vid, name, status, lead in named
    ]
    # Generic approved pool for the ~40 healthy SKUs.
    for i in range(1, 7):
        vid = f"Vendor-G{i:02d}"
        vendors.append(
            VendorMaster(
                vendor_id=vid,
                vendor_name=f"General Supplier {i:02d}",
                status="APPROVED",
                contact_email=f"orders@g{i:02d}.example.com",
                reliability_score=0.9,
                default_lead_time_days=int(7 + i),
            )
        )
    return vendors


# ---------------------------------------------------------------------------
# Scenario inventory + approvals
# ---------------------------------------------------------------------------
def build_scenarios(rng: np.random.Generator) -> tuple[
    list[Inventory], list[VendorApproval], list[SalesOfftake], list[PromoCalendar], list[OpenPO]
]:
    """Build the inventory, approvals, offtake, promos, and open POs for S1-S4.

    Args:
        rng: Seeded numpy generator for the offtake histories.

    Returns:
        Tuple of (inventory, approvals, offtake, promos, open_pos).

    Rationale: encodes every scenario anchor in one place so the values in the
    spec map one-to-one onto rows you can eyeball with show_db.py.
    """
    inv: list[Inventory] = []
    appr: list[VendorApproval] = []
    offtake: list[SalesOfftake] = []
    promos: list[PromoCalendar] = []
    open_pos: list[OpenPO] = []

    # ---- S1: SKU-123 Detergent, auto-issue ---------------------------------
    inv.append(Inventory(
        sku="SKU-123", description="Ultra Clean Detergent 5kg", category="Household",
        on_hand=50, reorder_threshold=100, pack_size=100, uom="EA",
        primary_vendor_id="Vendor-ABC", lead_time_days=7, lead_multiplier=1.0,
        peak_season=False, safety_stock=20, updated_at=REFERENCE_DATE,
    ))
    appr.append(VendorApproval(
        vendor_id="Vendor-ABC", sku="SKU-123", approval_status="APPROVED",
        is_primary=True, moq=500, unit_cost=2.50, lead_time_days=7,
        approved_on=REFERENCE_DATE - timedelta(days=200),
    ))
    offtake += _offtake_rows("SKU-123", 608, rng)
    promos.append(PromoCalendar(
        sku="SKU-123", promo_name="Summer Detergent Deal", promo_type="PRICE",
        uplift_pct=0.15, start_date=REFERENCE_DATE - timedelta(days=8),
        end_date=REFERENCE_DATE + timedelta(days=21),
    ))

    # ---- S2: SKU-456 Shampoo, draft-for-approval ---------------------------
    inv.append(Inventory(
        sku="SKU-456", description="Silk Shine Shampoo 1L", category="Personal Care",
        on_hand=30, reorder_threshold=150, pack_size=24, uom="EA",
        primary_vendor_id="Vendor-XYZ", lead_time_days=10, lead_multiplier=1.0,
        peak_season=False, safety_stock=30, updated_at=REFERENCE_DATE,
    ))
    # Primary vendor is PENDING; the approved backup (LMN) carries NO row for
    # SKU-456 -> no approved vendor exists -> draft path.
    appr.append(VendorApproval(
        vendor_id="Vendor-XYZ", sku="SKU-456", approval_status="PENDING",
        is_primary=True, moq=300, unit_cost=3.20, lead_time_days=10,
        approved_on=None,
    ))
    offtake += _offtake_rows("SKU-456", 420, rng)
    promos.append(PromoCalendar(
        sku="SKU-456", promo_name="Shampoo BOGO", promo_type="BOGO",
        uplift_pct=0.25, start_date=REFERENCE_DATE - timedelta(days=4),
        end_date=REFERENCE_DATE + timedelta(days=25),
    ))

    # ---- S3: SKU-701..708 Confectionery, seasonal surge --------------------
    s3 = [
        ("SKU-701", "Chocolate Bars", "Vendor-PQR", 300, 120, 260, 1.10),
        ("SKU-702", "Gummy Bears", "Vendor-PQR", 300, 150, 300, 1.05),
        ("SKU-703", "Lollipops", "Vendor-PQR", 300, 90, 240, 1.00),
        ("SKU-704", "Toffees", "Vendor-STU", 250, 200, 280, 1.20),
        ("SKU-705", "Caramels", "Vendor-STU", 250, 160, 220, 1.15),
        ("SKU-706", "Mints", "Vendor-VWX", 200, 110, 310, 0.95),
        ("SKU-707", "Truffles", "Vendor-VWX", 200, 180, 250, 1.30),
        ("SKU-708", "Pralines", "Vendor-YZ1", 300, 140, 290, 1.25),
    ]
    bundling_promo_skus = {"SKU-701", "SKU-704", "SKU-706"}  # 3 with bundling promo
    for sku, name, vendor, moq, on_hand, weekly, cost in s3:
        inv.append(Inventory(
            sku=sku, description=name, category="Confectionery",
            on_hand=on_hand, reorder_threshold=400, pack_size=50, uom="EA",
            primary_vendor_id=vendor, lead_time_days=5, lead_multiplier=2.0,
            peak_season=True, safety_stock=50, updated_at=REFERENCE_DATE,
        ))
        appr.append(VendorApproval(
            vendor_id=vendor, sku=sku, approval_status="APPROVED",
            is_primary=True, moq=moq, unit_cost=cost, lead_time_days=5,
            approved_on=REFERENCE_DATE - timedelta(days=150),
        ))
        offtake += _offtake_rows(sku, weekly, rng)
        if sku in bundling_promo_skus:
            promos.append(PromoCalendar(
                sku=sku, promo_name="Diwali Sweet Bundle", promo_type="BUNDLE",
                uplift_pct=0.20, start_date=REFERENCE_DATE - timedelta(days=3),
                end_date=REFERENCE_DATE + timedelta(days=30),
            ))

    # ---- S4: SKU-212 Cooking Oil, suppression ------------------------------
    inv.append(Inventory(
        sku="SKU-212", description="Golden Cooking Oil 5L", category="Grocery",
        on_hand=80, reorder_threshold=200, pack_size=12, uom="EA",
        primary_vendor_id="Vendor-MNO", lead_time_days=14, lead_multiplier=1.0,
        peak_season=False, safety_stock=40, updated_at=REFERENCE_DATE,
    ))
    appr.append(VendorApproval(
        vendor_id="Vendor-MNO", sku="SKU-212", approval_status="APPROVED",
        is_primary=True, moq=200, unit_cost=1.80, lead_time_days=14,
        approved_on=REFERENCE_DATE - timedelta(days=220),
    ))
    offtake += _offtake_rows("SKU-212", 310, rng)
    # Open in-transit PO that already covers the threshold -> suppress.
    open_pos.append(OpenPO(
        po_number="PO-2025-00772", sku="SKU-212", vendor_id="Vendor-MNO",
        quantity=1500, status="IN-TRANSIT",
        order_date=REFERENCE_DATE - timedelta(days=12), eta_days=2,
    ))

    return inv, appr, offtake, promos, open_pos


# ---------------------------------------------------------------------------
# Healthy background SKUs
# ---------------------------------------------------------------------------
def build_healthy(rng: np.random.Generator) -> tuple[
    list[Inventory], list[VendorApproval], list[SalesOfftake]
]:
    """Build ~40 healthy SKUs that sit ABOVE threshold (no replenishment needed).

    Args:
        rng: Seeded numpy generator for randomised-but-reproducible values.

    Returns:
        Tuple of (inventory, approvals, offtake).

    Rationale: background noise proves the Stock Monitor only flags genuine
    breaches and the demo isn't four hand-picked rows in an empty warehouse.
    """
    inv: list[Inventory] = []
    appr: list[VendorApproval] = []
    offtake: list[SalesOfftake] = []
    pack_choices = [1, 6, 12, 24, 48]
    pool = [f"Vendor-G{i:02d}" for i in range(1, 7)]

    for n in range(40):
        sku = f"SKU-{300 + n}"
        category = SUBCATEGORIES[int(rng.integers(0, len(SUBCATEGORIES)))]
        threshold = int(rng.integers(80, 300))
        on_hand = threshold + int(rng.integers(60, 450))  # comfortably above
        vendor = pool[int(rng.integers(0, len(pool)))]
        weekly = float(rng.integers(100, 800))
        lead = int(rng.integers(5, 22))
        inv.append(Inventory(
            sku=sku, description=f"{category} Item {n:02d}", category=category,
            on_hand=on_hand, reorder_threshold=threshold,
            pack_size=int(pack_choices[int(rng.integers(0, len(pack_choices)))]),
            uom="EA", primary_vendor_id=vendor, lead_time_days=lead,
            lead_multiplier=1.0, peak_season=False,
            safety_stock=int(rng.integers(10, 60)), updated_at=REFERENCE_DATE,
        ))
        appr.append(VendorApproval(
            vendor_id=vendor, sku=sku, approval_status="APPROVED", is_primary=True,
            moq=int(rng.integers(50, 500)),
            unit_cost=round(float(rng.uniform(1.0, 20.0)), 2),
            lead_time_days=lead, approved_on=REFERENCE_DATE - timedelta(days=180),
        ))
        offtake += _offtake_rows(sku, weekly, rng)

    return inv, appr, offtake


# ---------------------------------------------------------------------------
# Season index & user directory
# ---------------------------------------------------------------------------
def build_seasons() -> list[SeasonIndex]:
    """Seasonal multipliers per sub-category (Confectionery +50% for Diwali).

    Returns:
        SeasonIndex rows; one per canonical sub-category.

    Rationale: deterministic seasonal uplift the forecaster applies on top of
    baseline offtake — the engine behind the S3 surge.
    """
    uplifts = {
        "Household": 0.10,
        "Personal Care": 0.15,
        "Confectionery": 0.50,
        "Grocery": 0.05,
        "Beverages": 0.08,
        "Snacks": 0.12,
        "Frozen": 0.00,
        "Bakery": 0.10,
    }
    rows: list[SeasonIndex] = []
    for category in SUBCATEGORIES:
        uplift = uplifts[category]
        label = "Diwali 2026" if category == "Confectionery" else "Summer 2026"
        rows.append(SeasonIndex(
            category=category, season_label=label,
            uplift_pct=uplift, factor=round(1.0 + uplift, 4),
        ))
    return rows


def build_users(fake: Faker) -> list[UserDirectory]:
    """One Demand Planner per sub-category, with slack + email.

    Args:
        fake: Seeded Faker instance for reproducible names.

    Returns:
        UserDirectory rows.

    Rationale: the Notification Agent maps a SKU's category to its owning DP.
    """
    users: list[UserDirectory] = []
    for category in SUBCATEGORIES:
        name = fake.first_name() + " " + fake.last_name()  # avoid prefixes/suffixes
        slug = name.lower().replace(" ", ".")
        users.append(UserDirectory(
            name=name, role="Demand Planner", subcategory=category,
            slack_handle="@" + slug,
            email=slug + "@distributor.example.com",
        ))
    return users


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def seed() -> None:
    """Drop, recreate, and fully seed poc.db deterministically.

    Rationale: a single entry point so `python scripts/seed_data.py` always
    yields the same four-scenario database.
    """
    rng = np.random.default_rng(RANDOM_SEED)
    Faker.seed(RANDOM_SEED)
    fake = Faker()

    log.info("Resetting schema at reference date %s (seed=%s)", REFERENCE_DATE, RANDOM_SEED)
    reset_db()

    vendors = build_vendors()
    s_inv, s_appr, s_offtake, promos, open_pos = build_scenarios(rng)
    h_inv, h_appr, h_offtake = build_healthy(rng)
    seasons = build_seasons()
    users = build_users(fake)

    inventory = s_inv + h_inv
    approvals = s_appr + h_appr
    offtake = s_offtake + h_offtake

    with get_session() as session:
        # Insert in FK dependency order (foreign keys are enforced).
        session.add_all(vendors)
        session.flush()
        session.add_all(inventory)
        session.flush()
        session.add_all(approvals)
        session.add_all(offtake)
        session.add_all(promos)
        session.add_all(seasons)
        session.add_all(open_pos)
        session.add_all(users)
        session.commit()

    log.info(
        "Seed complete: %d vendors, %d SKUs, %d offtake rows, %d approvals, "
        "%d promos, %d seasons, %d open POs, %d DPs.",
        len(vendors), len(inventory), len(offtake), len(approvals),
        len(promos), len(seasons), len(open_pos), len(users),
    )
    log.info("Scenario anchors: SKU-123 (S1), SKU-456 (S2), SKU-701..708 (S3), SKU-212 (S4).")


if __name__ == "__main__":
    seed()
