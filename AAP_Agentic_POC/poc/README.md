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
Set `USE_LLM=false` to force the fully deterministic template path for an offline demo.
See [`docs/llm.md`](docs/llm.md) for the exact prompts, the fallback chain, and how to
switch providers via `.env`.

## Project layout

```
poc/
  config.py          # shared seed + reference date + policy knobs + LLM settings
  data/              # SQLAlchemy models, engine/session, poc.db (generated)
  agents/            # the six agents + shared state, enums, db access, BY mock
  orchestration/     # LangGraph state machine + HITL interrupt
  llm/               # provider-agnostic LLM wrapper (Gemini -> Ollama -> template)
  ui/                # Streamlit DP dashboard (later)
  scripts/           # seed_data.py, show_db.py, smoke_agents.py, validate_phase*.py
  docs/              # data_model.md, agents.md, decision_logic.md, orchestration.md, llm.md
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

## The agents (Phase 1)

The six agents are deterministic Python functions sharing one typed state object
([`ReplenishmentState`](agents/state.py)); each reads/writes `poc.db` via
[`agents/db.py`](agents/db.py) and appends to the immutable `audit_log`. No LLM
and no orchestration framework yet — all detection, forecasting, and routing is
reproducible arithmetic. Verify the routing core end-to-end:

```bash
python scripts/seed_data.py      # deterministic seed (drops & recreates poc.db)
python scripts/smoke_agents.py   # routes SKU-123 / 456 / 212 through agents 1–4
```

Expected — and asserted by the script: `SKU-123 → AUTO-ISSUE`,
`SKU-456 → DRAFT-FOR-APPROVAL`, `SKU-212 → SUPPRESS`.

## Orchestration (Phase 2)

The six agents are wired into a **LangGraph state graph** with a conditional fork
on the autonomy tier and a real **human-in-the-loop pause** on the draft path
(LangGraph `interrupt` + checkpointer). The suppress branch skips the PO node
entirely; the draft branch reaches the PO node only after a human resume.

```bash
python scripts/seed_data.py        # deterministic seed
python scripts/validate_phase2.py  # re-seeds, then drives S1–S4 through the graph
```

```python
from orchestration import runner
runner.run_for_sku("SKU-123")                       # S1 auto-issue → completes
o = runner.run_for_sku("SKU-456", thread_id="d456") # S2 draft → pauses
runner.resume_run("d456", approved=True, approved_by="@dp")  # writes APPROVED PO
runner.run_for_sku("SKU-212")                       # S4 suppress → no PO
runner.run_batch([f"SKU-{n}" for n in range(701, 709)])      # S3 → 3 POs + 1 draft
```

See [`docs/orchestration.md`](docs/orchestration.md) for the graph diagram, the
state schema, and how interrupt/resume works.

## Documentation

- [`docs/data_model.md`](docs/data_model.md) — every table and column.
- [`docs/agents.md`](docs/agents.md) — each agent's input/output schema and rules.
- [`docs/decision_logic.md`](docs/decision_logic.md) — the conditions → route table.
- [`docs/orchestration.md`](docs/orchestration.md) — the LangGraph graph, state, and HITL interrupt.

## Status

- **Phase 0 — Repo scaffold + synthetic data layer:** ✅ complete.
- **Phase 1 — Deterministic agent core (no LLM, no framework):** ✅ complete —
  the six agents, shared state, the Blue Yonder `write_po()` mock + guardrail, and
  the `smoke_agents.py` routing check.
- **Phase 2 — LangGraph orchestration + autonomy routing:** ✅ complete — the six
  agents wired into a state graph with the three-way autonomy fork, the
  human-in-the-loop `interrupt`/resume on the draft path, batch consolidation
  (S3), and the `validate_phase2.py` acceptance gate (18 checks).
- **Phase 3 — LLM edges (justification + notification text):** ✅ complete — the
  provider-agnostic [`llm/provider.py`](llm/provider.py) wrapper (Gemini → Ollama →
  template, never raises), the LLM-written draft justification narrative (Approval
  Agent) and notification body (Notification Agent), the `USE_LLM` master switch,
  [`docs/llm.md`](docs/llm.md), and the `validate_phase3.py` gate (13 checks). The
  LLM never touches detection, forecasting, or routing.
- Later phases add the Streamlit dashboard.
