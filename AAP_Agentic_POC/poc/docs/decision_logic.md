# Decision logic — conditions → route

The Approval Agent routes every breached SKU to **exactly one** autonomy tier.
All inputs are computed deterministically by the upstream agents; this table is
the authoritative mapping. (`SS` = `vendor_master.status`; `AS` =
`vendor_approval.approval_status`.)

## Master routing table

| # | Stock signal | Primary approval row | `SS` / `AS` | qty ≥ MOQ | → Route | Reason |
|---|--------------|----------------------|-------------|-----------|---------|--------|
| 1 | `SUPPRESS` (effective ≥ threshold) | any | any | — | **SUPPRESS** | inbound supply already covers threshold |
| 2 | `LOW-STOCK` / `CRITICAL-LOW` | present, in-catalog | `APPROVED` / `APPROVED` | **yes** | **AUTO-ISSUE** | pre-approved vendor + MOQ cleared |
| 3 | `LOW-STOCK` / `CRITICAL-LOW` | present, in-catalog | `APPROVED` / `APPROVED` | **no** | **DRAFT** | `below-MOQ` |
| 4 | `LOW-STOCK` / `CRITICAL-LOW` | present | `SUSPENDED` *(either level)* | — | **DRAFT** | `suspended` |
| 5 | `LOW-STOCK` / `CRITICAL-LOW` | present | `PENDING` *(either level)* | — | **DRAFT** | `pending` |
| 6 | `LOW-STOCK` / `CRITICAL-LOW` | **absent** | — | — | **DRAFT** | `no-approved-vendor` |
| — | `HEALTHY` | — | — | — | *(skipped)* | `on_hand ≥ threshold` — not flagged |

**Precedence:**
1. **Suppression wins first.** If effective stock (on-hand + in-transit) already
   clears the threshold, the route is SUPPRESS regardless of vendor standing —
   no PO is ever written.
2. **Vendor-level status dominates per-SKU status.** A `SUSPENDED` parent vendor
   escalates to draft even when the per-SKU `approval_status` is `APPROVED`
   (row 4 — the S3 / SKU-708 / Vendor-YZ1 case).
3. **Draft-reason priority:** `no-approved-vendor` → `suspended` → `pending` →
   `below-MOQ` (the first that applies).

## Stock-signal classification (Stock Monitor)

| Condition | Signal |
|-----------|--------|
| `on_hand >= reorder_threshold` | `HEALTHY` (skipped) |
| `on_hand < threshold` **and** `on_hand + in_transit >= threshold` | `SUPPRESS` |
| `effective < threshold` **and** `on_hand < 0.25 × threshold` | `CRITICAL-LOW` |
| `effective < threshold` (otherwise) | `LOW-STOCK` |

## Write guardrail (PO Generator)

| Route | `human_approved` | Writes to Blue Yonder? | PO status |
|-------|------------------|------------------------|-----------|
| AUTO-ISSUE | n/a | **Yes** | `ISSUED` |
| DRAFT-FOR-APPROVAL | `True` | **Yes** | `APPROVED` |
| DRAFT-FOR-APPROVAL | `False` / `None` | No (awaiting approval) | — |
| SUPPRESS | n/a | **Never** | — |

## Notification routing (Notification Agent)

| Route | Channel | Urgency | Gist |
|-------|---------|---------|------|
| AUTO-ISSUE | Slack | normal | PO placed — FYI |
| DRAFT-FOR-APPROVAL | Email | high | action required: approve/reject |
| SUPPRESS | Slack | low | no action — covered by inbound supply |

## The four canonical scenarios

| Scenario | SKU(s) | Stock | Vendor | Route | Table row |
|----------|--------|-------|--------|-------|-----------|
| **S1** auto-issue | SKU-123 | 50/100, no inbound | Vendor-ABC `APPROVED`, MOQ 500 | **AUTO-ISSUE** | 2 |
| **S2** draft | SKU-456 | 30/150, no inbound | Vendor-XYZ `PENDING`, no approved backup | **DRAFT** (`pending`) | 5 |
| **S3** batch surge | SKU-701..708 | all below 400, peak Diwali ×2.0 | 701–707 approved (3 vendors) → consolidated; SKU-708 Vendor-YZ1 `SUSPENDED` | **AUTO-ISSUE ×7 + DRAFT** | 2 (×7), 4 (708) |
| **S4** suppression | SKU-212 | 80/200, **+1500 in-transit** (PO-2025-00772) | Vendor-MNO `APPROVED` | **SUPPRESS** | 1 |

> Tunable knobs (`CRITICAL_LOW_RATIO`, `DEFAULT_LEAD_MULTIPLIER`,
> `PEAK_LEAD_MULTIPLIER`, `SAFETY_BUFFER_FACTOR`) live in
> [`config.py`](../config.py) and are environment-overridable. None affect the
> *routes* of S1–S4 at their seeded values; they only tune quantities.
