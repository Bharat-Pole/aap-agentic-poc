# Guardrails (Phase 4)

This POC places real purchase orders into a mock Blue Yonder / SCPO system. The
guardrails below are what make that safe: they ensure an order is written **only**
when it is either pre-authorised or a human has explicitly approved it, that no
order is ever written on a false alarm, and that every decision is evidence in an
immutable audit trail. This document is the single reference for the three
guardrails: the **autonomy tiers**, the **PO-write guard**, and **required-field
validation**.

---

## 1. The autonomy tiers

The **Approval Agent** ([`agents/approval.py`](../agents/approval.py)) is the only
node that assigns a tier, and it assigns exactly one. The routing is deterministic
Python — no LLM is involved in the decision.

| Tier | Trigger (all deterministic) | Action | PO written? |
|------|------------------------------|--------|:-----------:|
| **AUTO-ISSUE** | genuine breach **and** vendor + SKU approved at both levels, in-catalog, MOQ cleared | PO generated and sent (`status=ISSUED`); DP notified after | **Yes** |
| **DRAFT-FOR-APPROVAL** | genuine breach but vendor pending/suspended, or no approved vendor carries the SKU | draft + justification; **graph pauses** for a human ruling | **Only if approved** (`status=APPROVED`) |
| **SUPPRESS** | effective stock (on-hand + in-transit) already clears the threshold | advisory + audit only | **Never** |

Suppression is checked **first** and overrides everything: covered stock can never
produce a PO. The order of precedence inside a genuine breach is vendor standing →
MOQ, and the draft reason recorded is the single highest-priority blocker
(`suspended` / `pending` / `no-approved-vendor` / `below-MOQ`).

### How the graph enforces it structurally

The autonomy fork is a conditional edge after the Approval node
([`orchestration/graph.py`](../orchestration/graph.py)):

```
approval ─┬─ AUTO-ISSUE         ─────────────────────► po_generator ─► notification ─► END
          ├─ DRAFT-FOR-APPROVAL ─► human_approval ────► po_generator ─► notification ─► END
          └─ SUPPRESS           ─────────────────────────────────────► notification ─► END
```

* **SUPPRESS skips the PO node entirely** — the guardrail cannot even be reached on
  a false alarm.
* **DRAFT routes through `human_approval`**, which calls LangGraph's `interrupt()`
  and *pauses the whole run before any PO write*. A draft physically cannot reach
  `po_generator` without an explicit human resume.

---

## 2. The PO-write guard

Even once a tier permits reaching the PO node, the **PO Generator**
([`agents/po_generator.py`](../agents/po_generator.py)) re-checks the guard in one
place — `_write_allowed(tier, human_approved)` — before any line is built. This is
defence in depth: the graph topology already protects the draft path, and this
function protects against any caller (batch path, a future direct call) too.

```python
def _write_allowed(tier, human_approved):
    if tier is AUTO_ISSUE:                      # pre-approved -> always writes
        return True, None
    if tier is DRAFT_FOR_APPROVAL:
        if human_approved:                      # the ONLY way a draft writes
            return True, None
        return False, "draft rejected/awaiting human approval — no write"
    return False, "suppressed — guardrail forbids writing a PO"   # never writes
```

The invariant, stated once: **a PO line reaches `write_po()` only when the route is
AUTO-ISSUE, or it is a DRAFT and `human_approved` is `True`.** This is the property
asserted exhaustively by `tests/test_guardrails.py::test_write_allowed_matrix`
across the full tier × approval matrix, and end-to-end by the SUPPRESS-writes-no-PO
and paused-draft-no-PO tests.

`write_po()` itself ([`agents/mock_blue_yonder.py`](../agents/mock_blue_yonder.py))
is a dumb, faithful "remote system": it records exactly what it is told and only
refuses an empty PO. **All policy lives in the one caller**, so the guard is easy to
audit.

---

## 3. Required-field validation

Permitting a write is necessary but not sufficient — the order must also be
*complete*. Before calling `write_po()`, the PO Generator runs
`_validate_required_fields(state, tier)` and **refuses loudly** (raises
`ValueError`) if anything required is missing:

| Field | Required for | Why |
|-------|--------------|-----|
| `vendor.primary_vendor_id` | every write | a PO must name a supplier |
| `forecast.qty_needed` > 0 | every write | a PO must order a positive quantity |
| `approved_by` | approved drafts only | the PO must be attributable to a human |

`POLineSpec` adds the field-level guarantees (`quantity > 0`, `unit_cost >= 0`) via
pydantic, so a malformed line cannot even be constructed.

---

## Human-in-the-loop: approve / reject

The orchestration layer ([`orchestration/runner.py`](../orchestration/runner.py))
exposes two verbs that resume a paused draft by its `run_id`:

```python
runner.approve(run_id, decision="@planner", note="ok to proceed")
runner.reject(run_id, reason="budget freeze this quarter")
```

* **`approve`** → `human_approved=True` → the PO Generator writes **exactly one**
  PO with `status=APPROVED`, attributed to the approver; the DP is notified that the
  PO was placed. An `APPROVAL_GRANTED` audit event records the approver and note.
* **`reject`** → `human_approved=False` → **no PO is written**; the
  `alternate_sourcing` flag is raised; an `APPROVAL_REJECTED` audit event records
  the reason; and the (mock) **procurement desk** is notified to source the SKU
  another way.

## SLA / escalation stub

When a breach is **CRITICAL-LOW** *and* it routes to a draft, the Approval Agent
stamps an `approval_deadline` (reference "today" + `CRITICAL_DRAFT_SLA_HOURS`,
default 24h) on the decision. There is no live timer in the POC — the deadline is
**stored** (on the decision and in the audit `details`) and **surfaced** on the
interrupt payload (`ApprovalRequest.approval_deadline` / `is_critical`) so the
dashboard can display it and a future scheduler could escalate against it
deterministically.

---

## Verifying the guardrails

```bash
python -m pytest                  # the guardrail / audit / HITL suites
python scripts/validate_phase4.py # the deterministic Phase-4 acceptance gate
```

See [`docs/audit.md`](audit.md) for the event types each guardrail emits and an
example trail for every scenario.
