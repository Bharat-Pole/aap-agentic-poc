"""Guardrail tests — the PO-write guard is the heart of Phase 4.

These prove the single invariant the whole POC rests on: a line reaches Blue
Yonder **only** when the route is AUTO-ISSUE, or it is a DRAFT that a human has
explicitly approved. Two layers are checked:

* the pure decision function :func:`agents.po_generator._write_allowed` across the
  full tier x human-approval matrix (fast, no DB);
* the same guarantee end to end through the compiled graph — a SUPPRESS run never
  creates a ``blue_yonder_po`` row, and a paused draft has no PO until approved.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agents.enums import AutonomyTier
from agents.po_generator import _write_allowed
from data.database import get_session
from data.models import BlueYonderPO
from orchestration import runner


def _po_rows(sku: str) -> int:
    """Count ``blue_yonder_po`` lines written for a SKU.

    Args:
        sku: The SKU to count.

    Returns:
        The number of persisted PO line rows referencing the SKU.

    Rationale: the guardrail is asserted at the execution table, not the API —
        the only honest measure of "was an order actually placed?".
    """
    with get_session() as s:
        return s.scalar(
            select(func.count()).select_from(BlueYonderPO).where(BlueYonderPO.sku == sku)
        )


@pytest.mark.parametrize(
    "tier,human_approved,expected_allowed",
    [
        (AutonomyTier.AUTO_ISSUE, None, True),       # pre-approved -> writes.
        (AutonomyTier.AUTO_ISSUE, False, True),      # human flag irrelevant to auto.
        (AutonomyTier.DRAFT_FOR_APPROVAL, True, True),   # the only way a draft writes.
        (AutonomyTier.DRAFT_FOR_APPROVAL, False, False),  # rejected -> refused.
        (AutonomyTier.DRAFT_FOR_APPROVAL, None, False),   # not yet ruled -> refused.
        (AutonomyTier.SUPPRESS, True, False),        # suppression never writes...
        (AutonomyTier.SUPPRESS, None, False),        # ...regardless of any flag.
    ],
)
def test_write_allowed_matrix(tier, human_approved, expected_allowed):
    """_write_allowed permits a write iff AUTO-ISSUE or (DRAFT and approved)."""
    allowed, reason = _write_allowed(tier, human_approved)
    assert allowed is expected_allowed
    # A refusal must always carry a reason; an approval must not.
    assert (reason is None) is expected_allowed


def test_suppress_run_writes_no_po(fresh_db):
    """A SUPPRESS run (S4: SKU-212) must never create a blue_yonder_po row."""
    outcome = runner.run_for_sku("SKU-212", run_id="g-s4", thread_id="g-s4")
    assert outcome.route is AutonomyTier.SUPPRESS
    assert outcome.po_written is False
    assert _po_rows("SKU-212") == 0


def test_paused_draft_has_no_po_until_approved(fresh_db):
    """A paused draft (S2: SKU-456) has no PO; approval then writes exactly one."""
    paused = runner.run_for_sku("SKU-456", run_id="g-s2", thread_id="g-s2")
    assert paused.paused_for_approval is True
    assert paused.po_written is False
    assert _po_rows("SKU-456") == 0  # nothing written while awaiting the human.

    done = runner.approve("g-s2", decision="@dp.test", note="ok to proceed")
    assert done.po_written is True
    assert _po_rows("SKU-456") == 1  # exactly one, only after approval.
