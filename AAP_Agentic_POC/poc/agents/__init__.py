"""Agent package — the six deterministic replenishment agents.

One module per agent, in pipeline order:

1. :mod:`agents.stock_monitor`   — breach / suppression detection.
2. :mod:`agents.demand_forecast` — demand sizing (+ promo/season uplift).
3. :mod:`agents.vendor_checker`  — supplier eligibility + consolidation helper.
4. :mod:`agents.approval`        — the autonomy-tier router (critical node).
5. :mod:`agents.po_generator`    — PO build + write guardrail.
6. :mod:`agents.notification`    — Demand Planner messaging.

All detection, forecasting, and routing here is **deterministic Python**; LLMs
are confined to draft-justification and notification phrasing in later phases.
Shared types live in :mod:`agents.enums` (enums), :mod:`agents.state`
(``ReplenishmentState``), and :mod:`agents.db` (data access + audit).

Import note: :mod:`agents.state` imports each agent's output model, so the agent
modules reference ``ReplenishmentState`` only under ``TYPE_CHECKING`` to keep the
import graph acyclic. Avoid importing ``agents.state`` at the top of an agent module.
"""

from __future__ import annotations
