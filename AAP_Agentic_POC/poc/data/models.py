"""SQLAlchemy ORM models — the SQLite schema for the replenishment POC.

Eleven tables mock the client's data estate end to end:

* Source/analytics (mock Palantir + Snowflake/AS400): ``inventory``,
  ``sales_offtake``, ``vendor_master``, ``vendor_approval``, ``promo_calendar``,
  ``season_index``, ``open_po``, ``user_directory``.
* Execution (mock Blue Yonder / SCPO): ``blue_yonder_po`` — written *only* on
  auto-issue or after explicit human approval, never on suppression.
* Cross-cutting: ``notifications`` (DP comms) and ``audit_log`` (immutable,
  append-only record of every routing decision, PO, and suppression).

Column choices follow the playbook data model (section 1.4); where the playbook
left a column under-specified, the intent is documented in docs/data_model.md.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by every model."""


# ---------------------------------------------------------------------------
# Inventory & demand signals
# ---------------------------------------------------------------------------
class Inventory(Base):
    """Current stock position and reorder policy for one SKU.

    The Stock Monitor Agent reads ``on_hand`` vs ``reorder_threshold`` to detect
    breaches; ``lead_time_days`` * ``lead_multiplier`` sets the planning horizon
    the Demand Forecast Agent sizes against.
    """

    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    reorder_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    uom: Mapped[str] = mapped_column(String, nullable=False, default="EA")
    primary_vendor_id: Mapped[str | None] = mapped_column(
        ForeignKey("vendor_master.vendor_id"), nullable=True
    )
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    lead_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    peak_season: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    safety_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[date] = mapped_column(Date, nullable=False)


class SalesOfftake(Base):
    """One day of unit sales for one SKU (the 90-day offtake history).

    The Demand Forecast Agent fits a statsmodels model on these rows; offtake is
    the only demand truth — promo/season are applied as deterministic uplifts.
    """

    __tablename__ = "sales_offtake"
    __table_args__ = (
        UniqueConstraint("sku", "sale_date", name="uq_offtake_sku_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(
        ForeignKey("inventory.sku"), nullable=False, index=True
    )
    sale_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    units_sold: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
class VendorMaster(Base):
    """A supplier and its overall standing.

    ``status`` is the vendor-level gate the Approval Agent honours: a SUSPENDED
    vendor escalates to draft even if a per-SKU approval row exists (S3/SKU-708).
    """

    __tablename__ = "vendor_master"

    vendor_id: Mapped[str] = mapped_column(String, primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String, nullable=False)
    # Overall standing: APPROVED | PENDING | SUSPENDED.
    status: Mapped[str] = mapped_column(String, nullable=False, default="APPROVED")
    contact_email: Mapped[str] = mapped_column(String, nullable=False)
    reliability_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.9
    )
    default_lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7
    )


class VendorApproval(Base):
    """A vendor's authorisation to supply a specific SKU (the approval registry).

    This is the table the Vendor Checker Agent queries. AUTO-ISSUE requires an
    ``approval_status`` of APPROVED *and* a non-suspended parent vendor. ``moq``
    lives here because minimum order quantity is a vendor+SKU contract term.
    """

    __tablename__ = "vendor_approval"
    __table_args__ = (
        UniqueConstraint("vendor_id", "sku", name="uq_approval_vendor_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendor_master.vendor_id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(
        ForeignKey("inventory.sku"), nullable=False, index=True
    )
    # Per-SKU approval: APPROVED | PENDING | SUSPENDED.
    approval_status: Mapped[str] = mapped_column(String, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    approved_on: Mapped[date | None] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------------------
# Demand modifiers
# ---------------------------------------------------------------------------
class PromoCalendar(Base):
    """A promotional uplift on a SKU over a date window.

    A promo is "active" when ``start_date <= REFERENCE_DATE <= end_date``. The
    Demand Forecast Agent multiplies baseline demand by ``(1 + uplift_pct)`` for
    active promos — deterministic, not LLM-driven.
    """

    __tablename__ = "promo_calendar"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(
        ForeignKey("inventory.sku"), nullable=False, index=True
    )
    promo_name: Mapped[str] = mapped_column(String, nullable=False)
    promo_type: Mapped[str] = mapped_column(String, nullable=False, default="PRICE")
    uplift_pct: Mapped[float] = mapped_column(Float, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)


class SeasonIndex(Base):
    """A seasonal demand multiplier for a sub-category.

    Stored as both an ``uplift_pct`` (e.g. 0.50 for Confectionery at Diwali) and
    the derived ``factor`` (1.50) for convenience. Applied multiplicatively by
    the Demand Forecast Agent.
    """

    __tablename__ = "season_index"
    __table_args__ = (
        UniqueConstraint("category", "season_label", name="uq_season_cat_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    season_label: Mapped[str] = mapped_column(String, nullable=False)
    uplift_pct: Mapped[float] = mapped_column(Float, nullable=False)
    factor: Mapped[float] = mapped_column(Float, nullable=False)


# ---------------------------------------------------------------------------
# Open / in-transit purchase orders (the suppression signal)
# ---------------------------------------------------------------------------
class OpenPO(Base):
    """An existing purchase order still inbound to the warehouse.

    IN-TRANSIT quantities add to "effective stock" (on_hand + in-transit). When
    effective stock already clears the threshold, the Approval Agent SUPPRESSES
    — no new PO (S4/PO-2025-00772).
    """

    __tablename__ = "open_po"

    po_number: Mapped[str] = mapped_column(String, primary_key=True)
    sku: Mapped[str] = mapped_column(
        ForeignKey("inventory.sku"), nullable=False, index=True
    )
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendor_master.vendor_id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lifecycle: OPEN | IN-TRANSIT | RECEIVED | CANCELLED.
    status: Mapped[str] = mapped_column(String, nullable=False, default="IN-TRANSIT")
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    eta_days: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Blue Yonder / SCPO execution target
# ---------------------------------------------------------------------------
class BlueYonderPO(Base):
    """A purchase order line written to the (mock) Blue Yonder / SCPO system.

    Lines that share a ``po_number`` form one consolidated PO for a vendor
    (S3 batch consolidation). Rows are created ONLY via ``write_po()`` on the
    auto-issue path, or after a human approves a draft — never on suppression.
    """

    __tablename__ = "blue_yonder_po"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(String, nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendor_master.vendor_id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(
        ForeignKey("inventory.sku"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Lifecycle: ISSUED (auto) | APPROVED (post human review).
    status: Mapped[str] = mapped_column(String, nullable=False, default="ISSUED")
    # Routing tier that produced this line: AUTO_ISSUE | DRAFT_FOR_APPROVAL.
    autonomy_tier: Mapped[str] = mapped_column(String, nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# Communications & audit
# ---------------------------------------------------------------------------
class Notification(Base):
    """A message rendered to a Demand Planner (no real email/Slack is sent).

    The Notification Agent uses an LLM only to phrase ``body``; routing facts in
    ``subject``/references are deterministic. Persisted so the dashboard can
    show the DP exactly what they would have received.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="slack")
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    po_number: Mapped[str | None] = mapped_column(String, nullable=True)
    autonomy_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="SENT")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuditLog(Base):
    """Immutable, append-only record of every decision the pipeline makes.

    DB triggers forbid UPDATE/DELETE (see database.py). ``details`` holds a JSON
    string with the deterministic inputs behind the decision, so any routing
    outcome can be reconstructed and defended.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    # ROUTING_DECISION | PO_WRITTEN | SUPPRESSED | DRAFT_CREATED | NOTIFIED | ...
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    sku: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    autonomy_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    po_number: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserDirectory(Base):
    """Demand Planners (and other recipients) the Notification Agent can target.

    One DP per sub-category; ``subcategory`` is how a SKU's category maps to the
    human who should be notified or asked to approve a draft.
    """

    __tablename__ = "user_directory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="Demand Planner")
    subcategory: Mapped[str] = mapped_column(String, nullable=False, index=True)
    slack_handle: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
