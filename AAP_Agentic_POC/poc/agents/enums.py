"""Shared enumerations for the agent pipeline.

Kept in their own dependency-free module so both the per-agent schemas and the
shared :class:`~agents.state.ReplenishmentState` can import them without creating
an import cycle. Every enum is a ``str`` enum so values serialise cleanly into
pydantic models, the SQLite string columns, and audit-log JSON.
"""

from __future__ import annotations

from enum import Enum


class StockSignal(str, Enum):
    """Outcome of the Stock Monitor's per-SKU breach evaluation.

    ``HEALTHY`` SKUs are skipped by the pipeline; ``SUPPRESS`` means in-transit
    supply already covers the threshold (a false alarm); ``LOW_STOCK`` and
    ``CRITICAL_LOW`` are genuine breaches that proceed to forecasting.
    """

    HEALTHY = "HEALTHY"
    LOW_STOCK = "LOW-STOCK"
    CRITICAL_LOW = "CRITICAL-LOW"
    SUPPRESS = "SUPPRESS"


class AutonomyTier(str, Enum):
    """The three autonomy tiers the Approval Agent routes to (exactly one).

    Values match ``blue_yonder_po.autonomy_tier`` in the schema. ``display``
    renders the hyphenated label used in the master spec and the dashboard.
    """

    AUTO_ISSUE = "AUTO_ISSUE"
    DRAFT_FOR_APPROVAL = "DRAFT_FOR_APPROVAL"
    SUPPRESS = "SUPPRESS"

    @property
    def display(self) -> str:
        """Return the human-facing label (``AUTO-ISSUE`` etc.).

        Returns:
            The tier name with underscores rendered as hyphens.

        Rationale: the schema/audit store underscored values; humans read hyphens.
        """
        return self.value.replace("_", "-")


class DraftReason(str, Enum):
    """Why a breach was routed to DRAFT-FOR-APPROVAL instead of auto-issued.

    Mutually exclusive; the Vendor Checker assigns the highest-priority reason
    that applies (suspended/pending dominate a below-MOQ quantity).
    """

    PENDING = "pending"
    SUSPENDED = "suspended"
    NO_APPROVED_VENDOR = "no-approved-vendor"
    BELOW_MOQ = "below-MOQ"


class Urgency(str, Enum):
    """Notification urgency, derived deterministically from the autonomy tier."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Channel(str, Enum):
    """Delivery channel for a (mock) Demand Planner notification."""

    SLACK = "slack"
    EMAIL = "email"
