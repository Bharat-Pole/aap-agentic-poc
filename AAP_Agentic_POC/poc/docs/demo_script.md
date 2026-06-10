# Demo script — the DP dashboard, click by click

A click-by-click walkthrough for demoing all four canonical scenarios on the
Streamlit Demand-Planner console. The whole demo runs from one button and a few
clicks; everything is deterministic, so it lands the same way every time.

> **Audience:** a Demand Planner and leadership. **Framing:** this mirrors the
> client's stack with local mocks — Palantir + Snowflake/AS400 are a single local
> SQLite database; Blue Yonder / SCPO is the `blue_yonder_po` table written through
> a `write_po()` stub. No real systems are touched.

---

## 0. Launch (one-time, before the room is watching)

From the `poc/` source root:

```bash
python scripts/seed_data.py     # deterministic seed (optional — the scan re-seeds too)
streamlit run ui/app.py
```

A browser tab opens at `http://localhost:8501`. You'll see the **DP Console** with
an empty worklist and the prompt *"Click ▶ Run 6 AM scan to start the demo."*

**Sidebar controls** (set these before you start):
- **Acting as** — the approver handle recorded on POs and the audit trail
  (default `@demand.planner`). Leave as-is or set a real-looking name.
- **Use LLM for narrative / notifications** — leave **on** to show machine-written
  text (Gemini → Ollama → template). Flip **off** any time for a byte-stable,
  offline run; routing is identical either way. If you have no API key and no local
  Ollama, leave it on anyway — it falls back to the template in ~1 second.

> **The scan is the reset point.** Each click of *Run 6 AM scan* re-seeds the
> database, so the Blue Yonder panel starts empty every time and the four scenarios
> reproduce exactly. You can re-run the whole demo at will.

---

## 1. Run the 6 AM scan

**Click ▶ Run 6 AM scan** (sidebar).

A spinner runs ("Scanning warehouse and routing every breach…"), then:

- **Header metrics** update: ~45 SKUs in the warehouse, **11 flagged this scan**,
  and the Blue Yonder PO count jumps as auto-issues and consolidated POs are written.
- **🔎 6 AM scan** section lists what the Stock Monitor found, with a signal
  breakdown — **Critical-low**, **Low-stock**, **Suppress candidates** — and one row
  per flagged SKU tagged with its scenario (S1–S4). Point out that the ~40 healthy
  background SKUs are **not** here: the monitor only surfaces genuine breaches.

**Talking point:** *"One scheduled scan looked at the whole warehouse, flagged
every breach, and already acted on everything it was allowed to act on. What's left
on the worklist is only the things that need a human."*

Use the **Scenario filter** (sidebar radio: All / S1 / S2 / S3 / S4) to walk the
room through one scenario at a time. Set it to **All** to begin.

---

## 2. S1 — Auto-issue (watch the Blue Yonder write happen)

Filter to **S1** (or scroll to the `S1 · SKU-123` card).

The card shows:
- **On hand / threshold** `50 / 100` (LOW-STOCK), the **forecast order qty**, and
  the **forecast basis** caption (weekly average × promo × season × lead + buffer).
- **Vendor** `Vendor-ABC` — `APPROVED · APPROVED`.
- A green **AUTO-ISSUED** badge and a success line: *"Auto-issued PO-2026-… written
  to Blue Yonder; DP notified."*

**Now look right at the 🏭 Blue Yonder (mock SCPO) panel.** The PO line for SKU-123
is already there — status `ISSUED`, tier `AUTO_ISSUE`. 

**Talking point:** *"Vendor and SKU were pre-approved and the quantity cleared MOQ,
so the system placed the order itself and told the planner afterward. The PO you see
in Blue Yonder is the actual write — no human was in the loop."*

---

## 3. S2 — Draft for approval (read the justification, then approve)

Filter to **S2** (the `S2 · SKU-456` card).

The card shows an amber **NEEDS APPROVAL** badge and:
- `30 / 150` on hand (note this is **CRITICAL-LOW** → a red SLA line appears:
  *"action by …"*), the forecast qty, and **Vendor** `Vendor-XYZ` — `PENDING`.
- **Why this needs a human:** *pending* (the primary vendor is pending and no
  approved backup carries this SKU).
- **The LLM justification narrative** (blue info box) — a 3–4 sentence rationale a
  planner can read and defend. (Toggle the LLM off and re-scan to show this falling
  back to the deterministic template — same facts, no model.)
- **Last 90 days offtake** — an area chart of real daily sales behind the forecast.

Optionally type an **Approval note**. **Click ✅ Approve.**

The card flips to a green *"Approved — PO-2026-… written to Blue Yonder"* line, and
**the Blue Yonder panel gains a new row** — status `APPROVED`, `approved_by` = your
handle. The PO count and value tick up.

**Talking point:** *"Nothing was sent to the vendor until the planner approved. The
moment they did, the same guardrailed write path placed exactly one PO — and it's
attributed to a person in the audit trail."*

> To show the other half of the gate, re-scan and **Reject** the S2 card instead
> (a reason is required). No PO is written; the **Notifications** tab gains an
> *alternate-sourcing* notice addressed to the **procurement desk**.

---

## 4. S3 — Seasonal surge: consolidated POs + one escalation

Filter to **S3**. You'll see several cards:

- **Consolidated PO cards** (green **AUTO-ISSUED**), one per shared vendor
  (Vendor-PQR, Vendor-STU, Vendor-VWX). Each shows the **SKUs on the PO**, the **PO
  value**, and an expandable **line breakdown** (one row per SKU on the shared PO).
  Seven confectionery SKUs collapsed into **three vendor POs** — that's the
  consolidation win.
- **One escalation card** — `S3 · SKU-708` (Pralines), amber **NEEDS APPROVAL**.
  Its vendor `Vendor-YZ1` is **SUSPENDED**, so even though it's a real breach it
  cannot be auto-issued. It carries its own justification + 90-day chart and the
  same Approve / Reject controls.

Cross-check the **Blue Yonder panel**: you'll see the three consolidated PO numbers,
each with multiple SKU lines under one `po_number`. SKU-708 is **absent** — it has
no PO until a human acts.

**Talking point:** *"A seasonal spike hit eight SKUs at once. The system sized every
order, consolidated them into one PO per vendor to cut order overhead, and auto-issued
the seven it was cleared to. The eighth — suspended vendor — it refused to auto-issue
and put in front of a human. Scale and safety at the same time."*

Optionally **Approve** or **Reject** SKU-708 to show the escalation resolving (it
behaves exactly like S2).

---

## 5. S4 — Suppression (the false alarm it didn't act on)

Filter to **S4** (the `S4 · SKU-212` card).

A grey **SUPPRESSED** badge, and:
- `80 / 200` on hand — below threshold — but **effective stock** and **weeks of
  cover** are healthy.
- A warning line naming the in-transit PO: *"In-transit PO-2025-00772 (1500 units,
  IN-TRANSIT, ETA 2d) already lifts effective stock to … ≥ 200. Advisory + audit
  only."*

Confirm in the **Blue Yonder panel**: **no new PO for SKU-212**.

**Talking point:** *"On-hand looked low, so a naïve system would have ordered again.
This one saw the 1,500 units already in transit, recognized the breach as a false
alarm, and suppressed the order — no duplicate PO, no wasted cash. The reasoning is
in the audit trail."*

---

## 6. Notifications & Audit (close the loop)

On the right panel, switch tabs:

- **🔔 Notifications** — every message a planner (or procurement) would have
  received: auto-issue FYIs, the approval requests, the approved/placed confirmations,
  and any alternate-sourcing notice to procurement. One per routed SKU.
- **🧾 Audit trail** — the immutable, append-only record: every detection, routing
  decision, PO write, suppression, and human approve/reject, newest first. Note the
  caption — **UPDATE/DELETE are blocked at the database**, so this is tamper-evident
  evidence, not a convenience log.

**Closing talking point:** *"Every decision — what it acted on, what it held back,
who approved what, and why it suppressed an order — is recorded immutably. The
autonomy is bounded by a hard guardrail: a PO reaches Blue Yonder only on a
pre-approved auto-issue or an explicit human approval. Never on a suppression."*

---

## Quick reference — expected outcomes

| Scenario | Filter | Badge | Blue Yonder after scan | After human action |
|----------|--------|-------|------------------------|--------------------|
| **S1** SKU-123 | S1 | AUTO-ISSUED | 1 PO (`ISSUED`) | — |
| **S2** SKU-456 | S2 | NEEDS APPROVAL | none | Approve → 1 PO (`APPROVED`); Reject → none + procurement notice |
| **S3** SKU-701–708 | S3 | 3× AUTO-ISSUED + 1 NEEDS APPROVAL | 3 consolidated POs | SKU-708 Approve/Reject like S2 |
| **S4** SKU-212 | S4 | SUPPRESSED | none (and stays none) | — |

## Troubleshooting

- **Worklist empty / "Click Run 6 AM scan":** you haven't scanned yet — click the
  sidebar button.
- **A draft won't reject:** a rejection **reason** is required; type one first.
- **Narrative looks templated, not LLM-written:** the LLM toggle is off, or no
  provider was reachable (no `GEMINI_API_KEY` and no local Ollama). That's the
  intended graceful fallback — facts are identical. Set a key in `.env` (see
  [`llm.md`](llm.md)) and re-scan to show the machine-written version.
- **Want a clean slate mid-demo:** just click **Run 6 AM scan** again — it re-seeds.
