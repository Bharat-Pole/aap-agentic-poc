"""Mock Blue Yonder / SCPO execution system.

In production the PO Generator would call out to Blue Yonder / SCPO to place an
order. Here that external system is mocked by the ``blue_yonder_po`` table and
the :func:`write_po` stub below — the *only* function that inserts PO lines. The
guardrail (write only on auto-issue or human-approved draft, never on
suppression) is enforced by the PO Generator *before* it calls this stub; this
module is the dumb, faithful "remote system" that simply records what it's told.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from data.models import BlueYonderPO

log = logging.getLogger("agents.mock_blue_yonder")


class POLineSpec(BaseModel):
    """One line of a purchase order to be written to Blue Yonder."""

    sku: str
    quantity: int = Field(..., gt=0)
    unit_cost: float = Field(..., ge=0.0)


def write_po(
    session: Session,
    *,
    po_number: str,
    vendor_id: str,
    lines: list[POLineSpec],
    autonomy_tier: str,
    status: str,
    justification: str | None = None,
    approved_by: str | None = None,
    created_at: datetime | None = None,
) -> list[BlueYonderPO]:
    """Write PO lines to the (mock) Blue Yonder system.

    Args:
        session: Open ORM session (the caller commits).
        po_number: Shared PO number across all lines.
        vendor_id: Supplier the PO is placed with.
        lines: One or more line specs (multi-line = a consolidated PO).
        autonomy_tier: ``AUTO_ISSUE`` or ``DRAFT_FOR_APPROVAL``.
        status: ``ISSUED`` (auto) or ``APPROVED`` (post human review).
        justification: LLM/templated rationale (draft path only).
        approved_by: Human approver handle (draft path only).
        created_at: Override timestamp (defaults to ``datetime.now()``).

    Returns:
        The persisted (flushed) :class:`BlueYonderPO` line rows.

    Raises:
        ValueError: If ``lines`` is empty — an empty PO must never be written.

    Rationale: a single, dumb writer mirrors a real integration boundary, so the
        guardrail lives in exactly one caller (the PO Generator) and is easy to audit.
    """
    if not lines:
        raise ValueError("write_po called with no lines — refusing to write an empty PO.")

    ts = created_at or datetime.now()
    rows: list[BlueYonderPO] = []
    for line_no, line in enumerate(lines, start=1):
        row = BlueYonderPO(
            po_number=po_number,
            line_no=line_no,
            vendor_id=vendor_id,
            sku=line.sku,
            quantity=line.quantity,
            unit_cost=line.unit_cost,
            total_cost=round(line.quantity * line.unit_cost, 2),
            status=status,
            autonomy_tier=autonomy_tier,
            justification=justification,
            approved_by=approved_by,
            created_at=ts,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    log.info(
        "write_po %s vendor=%s lines=%d status=%s tier=%s",
        po_number, vendor_id, len(rows), status, autonomy_tier,
    )
    return rows
