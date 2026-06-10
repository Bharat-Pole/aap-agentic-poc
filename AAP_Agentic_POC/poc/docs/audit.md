# Audit trail (Phase 4)

Every routing decision, PO, suppression, and human ruling appends one row to the
`audit_log` table. The trail is the POC's evidence base: any outcome — including
*not* placing an order — can be reconstructed and defended from it.

## Append-only, in practice

`audit_log` is immutable by construction, not merely by convention:

* **One writer.** Every row goes through `db.append_audit()`
  ([`agents/db.py`](../agents/db.py)). Nothing in the codebase issues an `UPDATE` or
  `DELETE` against `audit_log`.
* **Enforced by the database.** Two SQLite triggers (`audit_log_no_update`,
  `audit_log_no_delete`, installed in [`data/database.py`](../data/database.py))
  `RAISE(ABORT, ...)` on any attempt to modify or remove a row — so even a buggy
  agent cannot rewrite history.

This is asserted by `tests/test_audit.py` (`test_audit_log_rejects_update`,
`test_audit_log_rejects_delete`).

## Row shape

| Column | Meaning |
|--------|---------|
| `run_id` | correlation id tying one pipeline run's rows together |
| `created_at` | timestamp |
| `agent` | the emitting agent (or `HumanApproval` for the human gate) |
| `event_type` | see the catalogue below |
| `sku`, `vendor_id`, `autonomy_tier`, `po_number` | the entities the event concerns |
| `summary` | one-line human-readable description |
| `details` | JSON: the deterministic inputs behind the decision |

## Event-type catalogue

| `event_type` | Emitted by | When | Notable `details` |
|--------------|-----------|------|-------------------|
| `STOCK_BREACH` | StockMonitorAgent | on-hand below threshold (genuine breach) | effective stock, signal |
| `SUPPRESS_CANDIDATE` | StockMonitorAgent | on-hand low but inbound supply covers it | effective stock, in-transit refs |
| `FORECAST` | DemandForecastAgent | demand sized (or advisory cover) | qty, uplifts, basis |
| `VENDOR_CHECK` | VendorCheckerAgent | vendor eligibility assessed | status, MOQ, draft reason |
| `ROUTING_DECISION` | ApprovalAgent | tier = **AUTO-ISSUE** | confidence, requires_human |
| `DRAFT_CREATED` | ApprovalAgent | tier = **DRAFT-FOR-APPROVAL** | justification payload, `is_critical`, `approval_deadline` |
| `SUPPRESSED` | ApprovalAgent | tier = **SUPPRESS** | **`effective_stock_calc`** (the full arithmetic) |
| `APPROVAL_GRANTED` | HumanApproval | a human approves a draft | approver, note |
| `APPROVAL_REJECTED` | HumanApproval | a human rejects a draft | rejecter, reason, `alternate_sourcing` |
| `PO_WRITTEN` | POGenerator | a PO line/consolidation is written | po lines, total, status |
| `PO_SKIPPED` | POGenerator | the guard withheld a write | skip reason |
| `NOTIFIED` | NotificationAgent | a DP / procurement notice is recorded | channel, urgency, recipient |

### The SUPPRESSED event carries its arithmetic

A suppression is the audit's hardest call to defend ("why was nothing ordered?"),
so the `SUPPRESSED` event embeds the exact effective-stock calculation:

```json
"effective_stock_calc": {
  "on_hand": 80,
  "in_transit_qty": 1500,
  "effective_stock": 1580,
  "reorder_threshold": 200,
  "covers_threshold": true,
  "in_transit_refs": [{"po_number": "PO-2025-00772", "quantity": 1500, "eta_days": 2, "status": "IN-TRANSIT"}]
}
```

Asserted by `test_suppressed_event_carries_effective_stock_calc`.

---

## Example trails (one per scenario)

Listed in order; agent names in brackets. Reproduce any of them by running the SKU
through the graph and reading `audit_log` filtered by `run_id`.

### S1 — auto-issue (SKU-123)

```
STOCK_BREACH      [StockMonitorAgent]   SKU-123 on-hand 50 < threshold 100
FORECAST          [DemandForecastAgent] sized order qty
VENDOR_CHECK      [VendorCheckerAgent]  Vendor-ABC APPROVED, MOQ cleared
ROUTING_DECISION  [ApprovalAgent]       AUTO-ISSUE
PO_WRITTEN        [POGenerator]         PO-2026-9000x ISSUED
NOTIFIED          [NotificationAgent]   DP notified (slack, normal)
```

### S2 — draft → approve (SKU-456)

```
STOCK_BREACH      [StockMonitorAgent]   on-hand 30 < threshold 150 (CRITICAL-LOW)
FORECAST          [DemandForecastAgent] sized order qty
VENDOR_CHECK      [VendorCheckerAgent]  Vendor-XYZ PENDING; no approved backup
DRAFT_CREATED     [ApprovalAgent]       DRAFT-FOR-APPROVAL; approval_deadline set (SLA)
── run pauses at the human-approval interrupt ──
APPROVAL_GRANTED  [HumanApproval]       approved by @planner — "ok to proceed"
PO_WRITTEN        [POGenerator]         PO-2026-9000x APPROVED
NOTIFIED          [NotificationAgent]   DP notified PO placed (email)
```

### S2 — draft → reject (SKU-456)

```
… STOCK_BREACH / FORECAST / VENDOR_CHECK / DRAFT_CREATED as above …
── run pauses at the human-approval interrupt ──
APPROVAL_REJECTED [HumanApproval]       rejected by @planner — "budget freeze"; alternate sourcing flagged
PO_SKIPPED        [POGenerator]         no PO — draft rejected by human
NOTIFIED          [NotificationAgent]   PROCUREMENT notified (email, high) — source elsewhere
```

### S3 — batch seasonal surge (SKU-701..708)

Per auto-issue SKU: `STOCK_BREACH → FORECAST → VENDOR_CHECK → ROUTING_DECISION`,
then **one `PO_WRITTEN` per shared vendor** (consolidated, multi-line) and a
`NOTIFIED` per SKU owner. SKU-708 (Vendor-YZ1 SUSPENDED) instead emits
`DRAFT_CREATED` and pauses — no PO until a human acts.

### S4 — suppression (SKU-212)

```
SUPPRESS_CANDIDATE [StockMonitorAgent]   on-hand 80 < 200 but effective 1580 ≥ 200
FORECAST           [DemandForecastAgent] advisory cover
VENDOR_CHECK       [VendorCheckerAgent]  (advisory; no order)
SUPPRESSED         [ApprovalAgent]       effective_stock_calc proves cover — NO PO
NOTIFIED           [NotificationAgent]   DP notified, no action (slack, low)
```

No `PO_WRITTEN` and no `PO_SKIPPED` from a write attempt ever appear on an S4 run —
the PO node is never reached.
