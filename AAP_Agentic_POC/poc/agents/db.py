"""Small data-access layer the agents use to read/write poc.db.

Every agent reads through these helpers rather than building ad-hoc queries, so
the SQL lives in one auditable place and the agents stay focused on decision
logic. The module also owns :func:`append_audit`, the single chokepoint for the
immutable audit trail every routing decision, PO, and suppression appends to.

All reads honour the fixed :data:`config.REFERENCE_DATE` (never wall-clock) so
the four canonical scenarios reproduce deterministically.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import PO_NUMBER_SEQ_BASE, REFERENCE_DATE
from data.models import (
    AuditLog,
    BlueYonderPO,
    Inventory,
    OpenPO,
    PromoCalendar,
    SalesOfftake,
    SeasonIndex,
    UserDirectory,
    VendorApproval,
    VendorMaster,
)

#: Open-PO statuses that count as inbound supply for the effective-stock sum.
IN_TRANSIT_STATUSES: frozenset[str] = frozenset({"IN-TRANSIT", "OPEN"})


# ---------------------------------------------------------------------------
# Inventory & demand reads
# ---------------------------------------------------------------------------
def get_inventory(session: Session, sku: str) -> Inventory | None:
    """Fetch one SKU's inventory row.

    Args:
        session: Open ORM session.
        sku: SKU id (e.g. ``SKU-123``).

    Returns:
        The :class:`Inventory` row, or ``None`` if the SKU is unknown.

    Rationale: the primary read every agent starts from.
    """
    return session.get(Inventory, sku)


def all_inventory(session: Session) -> list[Inventory]:
    """Return every inventory row, ordered by SKU.

    Args:
        session: Open ORM session.

    Returns:
        All :class:`Inventory` rows in stable SKU order.

    Rationale: the Stock Monitor scans the whole warehouse for breaches.
    """
    return list(session.scalars(select(Inventory).order_by(Inventory.sku)).all())


def intransit_open_pos(session: Session, sku: str) -> list[OpenPO]:
    """Return inbound (in-transit/open) POs for a SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to look up.

    Returns:
        Open-PO rows whose status counts as inbound supply.

    Rationale: their quantities add to effective stock — the suppression signal.
    """
    rows = session.scalars(select(OpenPO).where(OpenPO.sku == sku)).all()
    return [po for po in rows if po.status in IN_TRANSIT_STATUSES]


def offtake_units(session: Session, sku: str) -> list[int]:
    """Return the SKU's daily unit-sales history in date order.

    Args:
        session: Open ORM session.
        sku: SKU id to look up.

    Returns:
        A list of daily ``units_sold`` (oldest first); empty if no history.

    Rationale: the Demand Forecast Agent's only demand truth.
    """
    return list(
        session.scalars(
            select(SalesOfftake.units_sold)
            .where(SalesOfftake.sku == sku)
            .order_by(SalesOfftake.sale_date)
        ).all()
    )


def active_promo_uplift(
    session: Session, sku: str, ref_date: date = REFERENCE_DATE
) -> float:
    """Sum the uplift of all promos active on ``ref_date`` for a SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to look up.
        ref_date: The "today" a promo window is tested against.

    Returns:
        Total active ``uplift_pct`` (e.g. ``0.15`` for +15%); ``0.0`` if none.

    Rationale: deterministic demand modifier applied on top of baseline offtake.
    """
    rows = session.scalars(
        select(PromoCalendar.uplift_pct).where(
            PromoCalendar.sku == sku,
            PromoCalendar.start_date <= ref_date,
            PromoCalendar.end_date >= ref_date,
        )
    ).all()
    return float(sum(rows))


def season_uplift(session: Session, category: str) -> float:
    """Return the seasonal uplift for a sub-category.

    Args:
        session: Open ORM session.
        category: SKU sub-category (e.g. ``Confectionery``).

    Returns:
        The category's ``uplift_pct`` (e.g. ``0.50`` at Diwali); ``0.0`` if none.

    Rationale: the multiplicative seasonal factor behind the S3 surge.
    """
    row = session.scalar(
        select(SeasonIndex.uplift_pct).where(SeasonIndex.category == category)
    )
    return float(row) if row is not None else 0.0


# ---------------------------------------------------------------------------
# Vendor reads
# ---------------------------------------------------------------------------
def get_vendor(session: Session, vendor_id: str | None) -> VendorMaster | None:
    """Fetch a vendor's master record.

    Args:
        session: Open ORM session.
        vendor_id: Vendor id, or ``None``.

    Returns:
        The :class:`VendorMaster` row, or ``None``.

    Rationale: vendor-level status is the coarse gate before per-SKU approval.
    """
    if vendor_id is None:
        return None
    return session.get(VendorMaster, vendor_id)


def primary_approval(session: Session, sku: str) -> VendorApproval | None:
    """Return the primary vendor's approval row for a SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to look up.

    Returns:
        The ``is_primary`` :class:`VendorApproval` row, or ``None`` if the
        primary vendor does not carry the SKU.

    Rationale: the Vendor Checker's first port of call.
    """
    return session.scalar(
        select(VendorApproval).where(
            VendorApproval.sku == sku, VendorApproval.is_primary.is_(True)
        )
    )


def approved_alternatives(
    session: Session, sku: str, exclude_vendor: str | None = None
) -> list[VendorApproval]:
    """Return approval rows where an APPROVED, non-suspended vendor carries the SKU.

    Args:
        session: Open ORM session.
        sku: SKU id to look up.
        exclude_vendor: Vendor to omit (typically the primary).

    Returns:
        Approval rows whose per-SKU status is APPROVED *and* whose parent vendor
        is not suspended — i.e. usable fallback suppliers.

    Rationale: lets the Vendor Checker tell "primary is pending" apart from
        "no approved vendor exists at all" (the S2 distinction).
    """
    rows = session.scalars(
        select(VendorApproval).where(
            VendorApproval.sku == sku,
            VendorApproval.approval_status == "APPROVED",
        )
    ).all()
    alts: list[VendorApproval] = []
    for appr in rows:
        if appr.vendor_id == exclude_vendor:
            continue
        parent = get_vendor(session, appr.vendor_id)
        if parent is not None and parent.status != "SUSPENDED":
            alts.append(appr)
    return alts


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------
def dp_for_category(session: Session, category: str) -> UserDirectory | None:
    """Resolve the Demand Planner who owns a sub-category.

    Args:
        session: Open ORM session.
        category: SKU sub-category.

    Returns:
        The owning :class:`UserDirectory` row, or ``None`` if unmapped.

    Rationale: the Notification Agent targets the DP responsible for the SKU.
    """
    return session.scalar(
        select(UserDirectory).where(UserDirectory.subcategory == category)
    )


# ---------------------------------------------------------------------------
# PO numbering & audit
# ---------------------------------------------------------------------------
def next_po_number(session: Session, ref_date: date = REFERENCE_DATE) -> str:
    """Generate the next Blue Yonder PO number (``PO-<year>-<seq>``).

    Args:
        session: Open ORM session.
        ref_date: Reference date supplying the year segment.

    Returns:
        A PO number unique within ``blue_yonder_po`` for this run.

    Rationale: deterministic, gap-free numbering keyed off the rows already
        written, so consolidated POs and reruns stay collision-free.
    """
    existing = session.scalar(
        select(func.count(func.distinct(BlueYonderPO.po_number)))
    )
    seq = PO_NUMBER_SEQ_BASE + int(existing or 0) + 1
    return f"PO-{ref_date.year}-{seq:05d}"


def append_audit(
    session: Session,
    *,
    run_id: str | None,
    agent: str,
    event_type: str,
    summary: str,
    sku: str | None = None,
    vendor_id: str | None = None,
    autonomy_tier: str | None = None,
    po_number: str | None = None,
    details: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> AuditLog:
    """Append one immutable row to ``audit_log``.

    Args:
        session: Open ORM session (caller commits).
        run_id: Pipeline-run correlation id.
        agent: Name of the emitting agent.
        event_type: e.g. ``ROUTING_DECISION`` / ``PO_WRITTEN`` / ``SUPPRESSED``.
        summary: One-line human-readable summary.
        sku: SKU the event concerns, if any.
        vendor_id: Vendor the event concerns, if any.
        autonomy_tier: Routing tier, if applicable.
        po_number: PO the event concerns, if any.
        details: Deterministic decision inputs; serialised to JSON.
        created_at: Override timestamp (defaults to ``datetime.now()``).

    Returns:
        The persisted (flushed) :class:`AuditLog` row.

    Rationale: a single writer keeps every decision's evidence in one shape;
        the DB triggers then make it tamper-proof (no UPDATE/DELETE).
    """
    row = AuditLog(
        run_id=run_id,
        created_at=created_at or datetime.now(),
        agent=agent,
        event_type=event_type,
        sku=sku,
        vendor_id=vendor_id,
        autonomy_tier=autonomy_tier,
        po_number=po_number,
        summary=summary,
        details=json.dumps(details, default=str) if details is not None else None,
    )
    session.add(row)
    session.flush()
    return row
