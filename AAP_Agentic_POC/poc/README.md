# Agentic Inventory-Replenishment POC

A demoable proof-of-concept for an **agentic inventory-replenishment system** for an
automotive-parts distributor. Six agents detect stock breaches, forecast demand, check
vendor approval, **route by autonomy tier**, generate purchase orders, and notify the
Demand Planner — with a human-in-the-loop gate on anything that isn't pre-approved.

> **This is a dummy-data POC.** No real client schemas or system access. The client's
> stack is mirrored with local mocks: Palantir + Snowflake/AS400 → a single local SQLite
> database (`poc.db`); Blue Yonder / SCPO → a `blue_yonder_po` table written via a
> `write_po()` stub.

## The pipeline (six agents, fixed)

1. **Stock Monitor Agent** — detects SKUs at/under reorder threshold.
2. **Demand Forecast Agent** — forecasts demand (statsmodels) and applies promo + season uplift.
3. **Vendor Checker Agent** — looks up vendor + per-SKU approval status and MOQ.
4. **Approval Agent** — the decision node; routes to exactly one autonomy tier.
5. **PO Generator** — builds/consolidates POs and calls `write_po()`.
6. **Notification Agent** — phrases and delivers the message to the Demand Planner.

### Autonomy tiers (the Approval Agent picks one)

| Tier | Trigger | Action |
|------|---------|--------|
| **AUTO-ISSUE** | vendor + SKU approved **and** MOQ cleared | PO auto-generated and sent; DP notified after |
| **DRAFT-FOR-APPROVAL** | vendor pending/suspended or no approved vendor | draft PO + LLM justification; **pause for human approval** before any write |
| **SUPPRESS** | effective stock (on-hand + in-transit) already covers the threshold | no PO; advisory + audit only |

**Guardrail:** a PO is written to Blue Yonder **only** on auto-issue (pre-approved
vendor + SKU) or after a human explicitly approves a draft. **Never on suppression.**

### Determinism & the role of the LLM

All detection, forecasting, and routing is **deterministic Python**. The LLM is used
**only** for (a) drafting PO justification text on the draft path and (b) phrasing the
notification. The LLM wrapper degrades gracefully: **Gemini 2.5 Flash → local Ollama →
deterministic template**, so the demo never hard-fails and no API key is required to run.

## Project layout

```
poc/
  config.py          # shared seed + reference date + LLM settings
  data/              # SQLAlchemy models, engine/session, poc.db (generated)
  agents/            # one module per agent (added in later phases)
  orchestration/     # LangGraph state machine + HITL interrupt (later)
  llm/               # provider-agnostic LLM wrapper (later)
  ui/                # Streamlit DP dashboard (later)
  scripts/           # seed_data.py, show_db.py
  docs/              # data_model.md and design docs
```

## Setup

Requires **Python 3.11+**. From the `poc/` directory:

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

# 2. Install dependencies (free / open-source only)
pip install -r requirements.txt

# 3. (Optional) configure LLM keys — NOT needed for the data layer
cp .env.example .env        # then edit .env

# 4. Build and seed the database (deterministic; drops & recreates poc.db)
python scripts/seed_data.py

# 5. Eyeball the seed
python scripts/show_db.py
```

`scripts/show_db.py` should print row counts for all 11 tables and the scenario anchor
rows — **SKU-123 (S1), SKU-456 (S2), SKU-701..708 (S3), SKU-212 (S4)** — with the values
from the spec. Re-running `seed_data.py` always reproduces the same database (fixed
random seed + fixed reference date `2026-06-09`).

## The four scenarios

| # | SKU | Item | Stock | Routes to | Why |
|---|-----|------|-------|-----------|-----|
| **S1** | SKU-123 | Detergent | 50/100 | AUTO-ISSUE | Vendor-ABC approved, MOQ 500 cleared |
| **S2** | SKU-456 | Shampoo | 30/150 | DRAFT | Vendor-XYZ pending; no approved backup carries the SKU |
| **S3** | SKU-701..708 | Confectionery (Diwali) | all below | AUTO-ISSUE ×7 + escalate | season +50%; consolidate by vendor; YZ1/708 suspended → draft |
| **S4** | SKU-212 | Cooking Oil | 80/200 | SUPPRESS | open IN-TRANSIT PO-2025-00772 (1500u, ETA 2d) covers it |

## Documentation

- [`docs/data_model.md`](docs/data_model.md) — every table and column.

## Status

- **Phase 0 — Repo scaffold + synthetic data layer:** ✅ complete.
- Later phases add the LLM wrapper, the six agents, the LangGraph orchestration with the
  human-in-the-loop interrupt, and the Streamlit dashboard.
