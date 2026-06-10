"""Read-only views over poc.db for the dashboard panels.

Every function here issues a plain ``SELECT`` and returns plain data (a
``pandas.DataFrame`` or a list of dicts). Nothing in this module writes — the
*only* paths that mutate the database are the agent pipeline and the
approve/reject verbs, both reached through :mod:`ui.service`. Keeping the read
SQL in one module honours the phase requirement ("read-only DB views except via
the agent/approve paths") and keeps ``app.py`` free of query code.

The Blue Yonder, notifications, and audit panels read live on every Streamlit
rerun, so a PO written by an auto-issue or an approval shows up the instant the
table changes — the "watch the write happen" effect the client asked for.
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import get_session
from data.models import (
    AuditLog,
    BlueYonderPO,
    Inventory,
    Notification,
    OpenPO,
    SalesOfftake,
    VendorMaster,
)


# ---------------------------------------------------------------------------
# Demand history (the per-card 90-day chart)
# ---------------------------------------------------------------------------
def offtake_series(sku: str) -> pd.DataFrame:
    """Return a SKU's daily unit-sales history as a date-indexed frame.

    Args:
        sku: The SKU whose offtake to chart.

    Returns:
        A DataFrame indexed by ``sale_date`` with a single ``units_sold`` column
        (empty if the SKU has no history).

    Rationale: the draft-approval card plots this so the planner sees the last 90
        days of real offtake behind the forecast they are being asked to approve.
    """
    with get_session() as s:
        rows = s.execute(
            select(SalesOfftake.sale_date, SalesOfftake.units_sold)
            .where(SalesOfftake.sku == sku)
            .order_by(SalesOfftake.sale_date)
        ).all()
    if not rows:
        return pd.DataFrame(columns=["units_sold"])
    frame = pd.DataFrame(rows, columns=["sale_date", "units_sold"])
    return frame.set_index("sale_date")


# ---------------------------------------------------------------------------
# Blue Yonder (mock execution target)
# ---------------------------------------------------------------------------
def blue_yonder_lines() -> pd.DataFrame:
    """Return every written Blue Yonder PO line, newest first.

    Returns:
        A DataFrame of PO lines (po_number, line_no, vendor, sku, qty, costs,
        status, tier, approver, created_at); empty before any PO is written.

    Rationale: the side panel renders this verbatim so stakeholders see the exact
        rows that landed in the mock SCPO system after each action.
    """
    with get_session() as s:
        rows = s.execute(
            select(
                BlueYonderPO.po_number,
                BlueYonderPO.line_no,
                BlueYonderPO.vendor_id,
                BlueYonderPO.sku,
                BlueYonderPO.quantity,
                BlueYonderPO.unit_cost,
                BlueYonderPO.total_cost,
                BlueYonderPO.status,
                BlueYonderPO.autonomy_tier,
                BlueYonderPO.approved_by,
                BlueYonderPO.created_at,
            ).order_by(BlueYonderPO.created_at.desc(), BlueYonderPO.po_number, BlueYonderPO.line_no)
        ).all()
    cols = [
        "po_number", "line_no", "vendor_id", "sku", "quantity", "unit_cost",
        "total_cost", "status", "autonomy_tier", "approved_by", "created_at",
    ]
    return pd.DataFrame(rows, columns=cols)


def blue_yonder_summary() -> dict[str, float | int]:
    """Return headline counts for the Blue Yonder panel.

    Returns:
        ``{"pos": distinct PO numbers, "lines": line rows, "value": total cost}``.

    Rationale: a compact metric strip above the table makes the "N POs written"
        effect legible at a glance during the demo.
    """
    df = blue_yonder_lines()
    if df.empty:
        return {"pos": 0, "lines": 0, "value": 0.0}
    return {
        "pos": int(df["po_number"].nunique()),
        "lines": int(len(df)),
        "value": round(float(df["total_cost"].sum()), 2),
    }


def po_lines_for(po_number: str) -> pd.DataFrame:
    """Return the lines of one PO (for the consolidated-PO card breakdown).

    Args:
        po_number: The PO number to expand.

    Returns:
        A DataFrame of that PO's lines (sku, qty, unit_cost, total_cost).

    Rationale: the S3 consolidated card lists each SKU on the shared PO, which is
        exactly what makes "one order per vendor" visible.
    """
    with get_session() as s:
        rows = s.execute(
            select(
                BlueYonderPO.sku,
                BlueYonderPO.quantity,
                BlueYonderPO.unit_cost,
                BlueYonderPO.total_cost,
            )
            .where(BlueYonderPO.po_number == po_number)
            .order_by(BlueYonderPO.line_no)
        ).all()
    return pd.DataFrame(rows, columns=["sku", "quantity", "unit_cost", "total_cost"])


# ---------------------------------------------------------------------------
# Notifications feed
# ---------------------------------------------------------------------------
def notifications() -> pd.DataFrame:
    """Return the DP notifications feed, newest first.

    Returns:
        A DataFrame (created_at, channel, urgency-bearing subject, recipient,
        sku, po_number, tier, body).

    Rationale: the feed shows the planner exactly what they would have received
        on each channel for every routed SKU.
    """
    with get_session() as s:
        rows = s.execute(
            select(
                Notification.created_at,
                Notification.channel,
                Notification.subject,
                Notification.recipient,
                Notification.sku,
                Notification.po_number,
                Notification.autonomy_tier,
                Notification.body,
            ).order_by(Notification.created_at.desc(), Notification.id.desc())
        ).all()
    cols = [
        "created_at", "channel", "subject", "recipient", "sku",
        "po_number", "autonomy_tier", "body",
    ]
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def audit_rows(run_id: str | None = None) -> pd.DataFrame:
    """Return the immutable audit trail, newest first.

    Args:
        run_id: Optional filter to one run's rows.

    Returns:
        A DataFrame (created_at, run_id, agent, event_type, sku, vendor_id, tier,
        po_number, summary).

    Rationale: the audit tab is the evidence surface — every routing decision, PO,
        and suppression is appended here, and this is how the demo shows it.
    """
    stmt = select(
        AuditLog.created_at,
        AuditLog.run_id,
        AuditLog.agent,
        AuditLog.event_type,
        AuditLog.sku,
        AuditLog.vendor_id,
        AuditLog.autonomy_tier,
        AuditLog.po_number,
        AuditLog.summary,
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if run_id:
        stmt = stmt.where(AuditLog.run_id == run_id)
    with get_session() as s:
        rows = s.execute(stmt).all()
    cols = [
        "created_at", "run_id", "agent", "event_type", "sku",
        "vendor_id", "autonomy_tier", "po_number", "summary",
    ]
    return pd.DataFrame(rows, columns=cols)


def audit_detail(run_id: str, event_type: str) -> dict:
    """Return the parsed ``details`` JSON of one audit row (or empty).

    Args:
        run_id: The run correlation id.
        event_type: The audit event type to fetch (e.g. ``'SUPPRESSED'``).

    Returns:
        The decoded ``details`` dict, or ``{}`` if absent/unparseable.

    Rationale: the suppression card surfaces the effective-stock arithmetic the
        ``SUPPRESSED`` event recorded — the audit's defence against "why no PO?".
    """
    with get_session() as s:
        raw = s.scalar(
            select(AuditLog.details).where(
                AuditLog.run_id == run_id, AuditLog.event_type == event_type
            )
        )
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Reference reads (card context)
# ---------------------------------------------------------------------------
def inventory_overview() -> dict[str, int]:
    """Return whole-warehouse counts for the header metric strip.

    Returns:
        ``{"skus": total SKUs, "vendors": total vendors}``.

    Rationale: framing the four flagged SKUs against the full warehouse proves the
        monitor isn't just looking at a hand-picked handful.
    """
    with get_session() as s:
        n_skus = s.scalar(_count(Inventory)) or 0
        n_vendors = s.scalar(_count(VendorMaster)) or 0
    return {"skus": int(n_skus), "vendors": int(n_vendors)}


def open_pos_for(sku: str) -> pd.DataFrame:
    """Return inbound open POs for a SKU (the suppression evidence).

    Args:
        sku: The SKU to look up.

    Returns:
        A DataFrame (po_number, vendor_id, quantity, status, eta_days).

    Rationale: the suppression card cites the exact in-transit PO that made the
        new order unnecessary.
    """
    with get_session() as s:
        rows = s.execute(
            select(
                OpenPO.po_number, OpenPO.vendor_id, OpenPO.quantity,
                OpenPO.status, OpenPO.eta_days,
            ).where(OpenPO.sku == sku)
        ).all()
    return pd.DataFrame(
        rows, columns=["po_number", "vendor_id", "quantity", "status", "eta_days"]
    )


def _count(model) -> "select":
    """Build a ``SELECT count(*)`` statement for a model.

    Args:
        model: The ORM model to count.

    Returns:
        A SQLAlchemy select expression yielding the row count.

    Rationale: a tiny shared helper keeps the overview reads terse and uniform.
    """
    from sqlalchemy import func

    return select(func.count()).select_from(model)
