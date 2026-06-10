# Agents (Phase 1 — deterministic core)

The six agents are pure, deterministic Python functions for everything that
matters to a decision. **No LLM touches detection, forecasting, or routing** —
every such decision is reproducible arithmetic. The LLM is used at only two
human-facing edges, both behind a deterministic template fallback: the draft
justification narrative (Approval Agent) and the notification body (Notification
Agent). See [`llm.md`](llm.md). The LangGraph human-in-the-loop interrupt is
covered in [`orchestration.md`](orchestration.md).

Each agent lives in `agents/<name>.py`, defines a pydantic **input model** and
**output model**, and exposes `run(state, session=None) -> state`. All agents
share one typed state object, [`ReplenishmentState`](../agents/state.py), and
read/write `poc.db` through the data-access helpers in
[`agents/db.py`](../agents/db.py). Every decision appends to the immutable
`audit_log`.

## Shared state

One `ReplenishmentState` flows through the pipeline (one instance per SKU run).
Each agent fills its own slot:

| Field | Filled by | Type |
|-------|-----------|------|
| `stock` | Stock Monitor | `StockStatus` |
| `forecast` | Demand Forecast | `ForecastResult` |
| `vendor` | Vendor Checker | `VendorAssessment` |
| `decision` | Approval | `ApprovalDecision` |
| `po` | PO Generator | `POResult` |
| `notification` | Notification | `NotificationResult` |
| `human_approved` / `approved_by` | (human, draft path) | `bool` / `str` |

Construct one with `agents.state.new_state(sku)`.

## Enums

Defined in [`agents/enums.py`](../agents/enums.py):

- `StockSignal` — `HEALTHY` · `LOW-STOCK` · `CRITICAL-LOW` · `SUPPRESS`
- `AutonomyTier` — `AUTO_ISSUE` · `DRAFT_FOR_APPROVAL` · `SUPPRESS` (`.display`
  renders the hyphenated label)
- `DraftReason` — `pending` · `suspended` · `no-approved-vendor` · `below-MOQ`
- `Urgency`, `Channel` — notification routing

---

## 1. Stock Monitor — `agents/stock_monitor.py`

Detects breaches against the reorder policy using **effective stock**
(`on_hand + in-transit open-PO quantity`).

- **Input** `StockMonitorInput{ sku }`
- **Output** `StockStatus{ sku, on_hand, reorder_threshold, in_transit_qty,
  effective_stock, signal, is_breach, in_transit_refs[], note }`
- **Rules**
  - `on_hand >= threshold` → `HEALTHY` (skipped by the pipeline)
  - `on_hand < threshold` **and** `effective_stock >= threshold` → `SUPPRESS`
  - `effective_stock < threshold` → `LOW-STOCK`, or `CRITICAL-LOW` when
    `on_hand < CRITICAL_LOW_RATIO × threshold` (default 0.25)
- **Extras** `scan_all(session)` returns every flagged SKU (healthy skipped) —
  the seed for per-SKU runs.

## 2. Demand Forecast — `agents/demand_forecast.py`

Sizes the order from 90-day offtake plus deterministic uplifts.

- **Input** `DemandForecastInput{ sku, advisory_only }`
- **Output** `ForecastResult{ weekly_avg, daily_avg, promo_uplift_pct,
  season_uplift_pct, combined_uplift, lead_multiplier, safety_buffer,
  qty_needed, days_to_stockout, weeks_of_cover, advisory, forecast_basis }`
- **Rules**
  - `weekly_avg = mean(daily offtake) × 7`
  - `combined_uplift = (1 + promo) × (1 + season)`
  - `lead_multiplier`: the SKU's `inventory.lead_multiplier` when it is an
    explicit override (> 1.0), else the policy default — **1.5** normal / **2.0**
    peak-season
  - `safety_buffer = safety_stock × SAFETY_BUFFER_FACTOR` (default ×1.0)
  - `qty_needed = ceil_to_pack(weekly_avg × combined_uplift × lead_multiplier +
    safety_buffer)`
  - `days_to_stockout = on_hand ÷ daily_avg`
  - **Advisory mode** (SUPPRESS SKUs, selected from the upstream signal):
    `qty_needed = None`; reports `weeks_of_cover = effective_stock ÷ weekly_avg`

## 3. Vendor Checker — `agents/vendor_checker.py`

Resolves supplier eligibility and **recommends** a route (the Approval Agent
makes the final call).

- **Input** `VendorCheckerInput{ sku, qty_needed }`
- **Output** `VendorAssessment{ primary_vendor_id, vendor_status,
  approval_status, in_catalog, moq, unit_cost, lead_time_days, qty_meets_moq,
  approved_alternative_exists, recommended_route, draft_reason, note }`
- **Rules** — recommend `AUTO_ISSUE` iff **all** hold:
  1. primary vendor carries the SKU (`in_catalog`)
  2. `vendor_master.status == APPROVED`
  3. `vendor_approval.approval_status == APPROVED`
  4. `qty_needed >= moq` (skipped when `qty_needed is None`)

  Otherwise recommend `DRAFT_FOR_APPROVAL` with the highest-priority
  `draft_reason`: **no-approved-vendor** (no primary approval row) →
  **suspended** → **pending** → **below-MOQ**.
- **Extras** `consolidate_by_vendor(assessments)` groups the AUTO-ISSUE SKUs by
  shared vendor into one PO group each (S3); draft/escalated SKUs are excluded.

## 4. Approval — `agents/approval.py` (critical routing node)

Combines the three upstream verdicts into exactly one autonomy tier.

- **Input** `ApprovalInput{ stock, forecast, vendor }`
- **Output** `ApprovalDecision{ tier, reason, confidence, requires_human,
  justification_payload, note }`
- **Rules** (priority order)
  1. `stock.signal == SUPPRESS` → **SUPPRESS** (overrides everything; never a PO)
  2. else `vendor.recommended_route == AUTO_ISSUE` → **AUTO_ISSUE**
  3. else → **DRAFT_FOR_APPROVAL**, `requires_human = True`, and assemble the
     **structured** `justification_payload` (on-hand, threshold,
     days-to-stockout, promo/season pressure, vendor reason, MOQ, cost…). *Text
     generation is a later phase; Phase 1 emits only the structured fields.*
- `confidence` is a fixed, deterministic value per outcome (suppress 0.97,
  auto 0.95, draft 0.60–0.80 by reason) — explainable, not probabilistic.

## 5. PO Generator — `agents/po_generator.py`

Builds POs and **enforces the write guardrail**. Calls
[`mock_blue_yonder.write_po()`](../agents/mock_blue_yonder.py) — the only
function that inserts into `blue_yonder_po`.

- **Input** `POGeneratorInput{ sku, human_approved, approved_by }`
- **Output** `POResult{ po_number, vendor_id, autonomy_tier, status, lines[],
  total_cost, written, skipped_reason }`
- **Guardrail** — a PO is written **only** when:
  - tier is `AUTO_ISSUE` → status **ISSUED**, or
  - tier is `DRAFT_FOR_APPROVAL` **and** `human_approved` → status **APPROVED**
  - **never** on `SUPPRESS` (and a draft without approval is skipped, not written)
- **Extras** `generate_consolidated(...)` writes one multi-line PO per vendor for
  the S3 batch.

## 6. Notification — `agents/notification.py`

Resolves the SKU's owning Demand Planner and records the message.

- **Input** `NotificationInput{ sku }`
- **Output** `NotificationResult{ recipient, channel, urgency, subject, body,
  sku, po_number, autonomy_tier, status }`
- **Rules** (deterministic; Phase 1 uses plain templates)
  - `AUTO_ISSUE` → Slack, **normal** ("PO placed, FYI")
  - `DRAFT_FOR_APPROVAL` → Email, **high** ("action required")
  - `SUPPRESS` → Slack, **low** ("no action; covered by inbound supply")

---

## Audit events

Every agent appends to `audit_log` (immutable; UPDATE/DELETE blocked by DB
triggers). Event types emitted in Phase 1:

| Agent | `event_type` |
|-------|--------------|
| Stock Monitor | `STOCK_BREACH` / `SUPPRESS_CANDIDATE` |
| Demand Forecast | `FORECAST` |
| Vendor Checker | `VENDOR_CHECK` |
| Approval | `ROUTING_DECISION` (auto) / `DRAFT_CREATED` (draft) / `SUPPRESSED` |
| PO Generator | `PO_WRITTEN` / `PO_SKIPPED` |
| Notification | `NOTIFIED` |

`details` carries the JSON of the deterministic decision inputs, so any outcome
can be reconstructed and defended.

## Verifying

```bash
python scripts/seed_data.py      # deterministic seed
python scripts/smoke_agents.py   # routes SKU-123/456/212 through agents 1–4
```

Expected: `SKU-123 → AUTO-ISSUE`, `SKU-456 → DRAFT-FOR-APPROVAL`,
`SKU-212 → SUPPRESS`. The script asserts these and exits non-zero on drift.
