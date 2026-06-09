# Data model (`poc.db`)

`poc.db` is a single local SQLite database that mocks the client's data estate:
**Palantir** (forecast/analytics inputs) and the **Snowflake on AWS** warehouse fed by
**AS400** are collapsed into the source tables below; **Blue Yonder / SCPO** (PO
execution) is mocked by `blue_yonder_po`. Schema is defined in
[`data/models.py`](../data/models.py) via SQLAlchemy 2.0 models and created/seeded by
[`scripts/seed_data.py`](../scripts/seed_data.py).

All data is synthetic and deterministic: a fixed random seed (`42`) and a fixed reference
date (`2026-06-09`, see [`config.py`](../config.py)) make every run reproducible.

> Where the playbook (section 1.4) left a column under-specified, this document records
> the interpretation used. Notable additions: `pack_size` on `inventory` (per the Phase 0
> instruction) and `lead_multiplier` / `peak_season` to support the S3 seasonal surge.

## Conventions

- **SKU** ids look like `SKU-123`; **vendor** ids like `Vendor-ABC`.
- **Status enumerations** are stored as plain strings (SQLite has no enum type); allowed
  values are listed per column below.
- **Dates** are stored as `DATE`; event timestamps as `DATETIME`.
- Foreign keys are enforced (`PRAGMA foreign_keys=ON`).

---

## `inventory`
Current stock position and reorder policy for one SKU. Read by the **Stock Monitor Agent**
(breach detection) and the **Demand Forecast Agent** (planning horizon).

| Column | Type | Notes |
|--------|------|-------|
| `sku` (PK) | str | e.g. `SKU-123` |
| `description` | str | Human-readable product name |
| `category` | str | Sub-category; one of the eight canonical values |
| `on_hand` | int | Units physically in the warehouse |
| `reorder_threshold` | int | Breach when `on_hand <= reorder_threshold` |
| `pack_size` | int | Units per pack (rounding unit for order qty) |
| `uom` | str | Unit of measure (default `EA`) |
| `primary_vendor_id` | str → `vendor_master.vendor_id` | Default supplier |
| `lead_time_days` | int | Base replenishment lead time |
| `lead_multiplier` | float | Peak-season lead inflation (e.g. `2.0` for S3) |
| `peak_season` | bool | Whether the SKU is in a peak window |
| `safety_stock` | int | Buffer added on top of forecast demand |
| `updated_at` | date | Snapshot date (= reference date in seed) |

## `sales_offtake`
Daily unit-sales history (90 days per SKU). The only demand "truth"; the
**Demand Forecast Agent** fits a statsmodels model on it.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `sku` → `inventory.sku` | str | |
| `sale_date` | date | Unique per `(sku, sale_date)` |
| `units_sold` | int | Non-negative |

## `vendor_master`
A supplier and its **overall** standing. A `SUSPENDED` vendor escalates to draft even when
a per-SKU approval exists (S3 / SKU-708 / Vendor-YZ1).

| Column | Type | Notes |
|--------|------|-------|
| `vendor_id` (PK) | str | e.g. `Vendor-ABC` |
| `vendor_name` | str | |
| `status` | str | `APPROVED` \| `PENDING` \| `SUSPENDED` |
| `contact_email` | str | |
| `reliability_score` | float | 0–1 |
| `default_lead_time_days` | int | |

## `vendor_approval`
The **approval registry**: a vendor's authorisation to supply a *specific* SKU. Queried by
the **Vendor Checker Agent**. AUTO-ISSUE requires `approval_status = APPROVED` **and** a
non-suspended parent vendor. MOQ lives here because it is a vendor+SKU contract term.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `vendor_id` → `vendor_master.vendor_id` | str | Unique per `(vendor_id, sku)` |
| `sku` → `inventory.sku` | str | |
| `approval_status` | str | `APPROVED` \| `PENDING` \| `SUSPENDED` |
| `is_primary` | bool | Preferred vendor for the SKU |
| `moq` | int | Minimum order quantity |
| `unit_cost` | float | Per-unit cost |
| `lead_time_days` | int | Vendor-specific lead time |
| `approved_on` | date \| null | Null while pending |

> **S2 note:** there is no `vendor_approval` row linking the approved backup `Vendor-LMN`
> to `SKU-456` — i.e. the approved vendor does not carry the SKU — so the only candidate is
> the `PENDING` `Vendor-XYZ`, forcing the draft path.

## `promo_calendar`
A promotional uplift on a SKU over a date window. Active when
`start_date <= reference_date <= end_date`. The **Demand Forecast Agent** multiplies
baseline demand by `(1 + uplift_pct)` for active promos.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `sku` → `inventory.sku` | str | |
| `promo_name` | str | |
| `promo_type` | str | `PRICE` \| `BOGO` \| `BUNDLE` \| … |
| `uplift_pct` | float | e.g. `0.15` for +15% |
| `start_date` / `end_date` | date | Active window |

## `season_index`
Seasonal demand multiplier per sub-category. Stored as both `uplift_pct` and the derived
`factor` (`1 + uplift_pct`).

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `category` | str | Unique per `(category, season_label)` |
| `season_label` | str | e.g. `Diwali 2026` |
| `uplift_pct` | float | e.g. `0.50` for Confectionery |
| `factor` | float | `1 + uplift_pct` |

## `open_po`
Existing purchase orders still inbound. IN-TRANSIT quantities add to **effective stock**
(`on_hand + in-transit`). When effective stock already clears the threshold, the
**Approval Agent** SUPPRESSES (S4 / PO-2025-00772).

| Column | Type | Notes |
|--------|------|-------|
| `po_number` (PK) | str | e.g. `PO-2025-00772` |
| `sku` → `inventory.sku` | str | |
| `vendor_id` → `vendor_master.vendor_id` | str | |
| `quantity` | int | Units inbound |
| `status` | str | `OPEN` \| `IN-TRANSIT` \| `RECEIVED` \| `CANCELLED` |
| `order_date` | date | |
| `eta_days` | int | Days until arrival |

## `blue_yonder_po`  *(execution target — mock Blue Yonder / SCPO)*
PO **lines** written via `write_po()`. Lines sharing a `po_number` form one consolidated
PO for a vendor (S3 batch consolidation). Rows are created **only** on the auto-issue path
or after a human approves a draft — **never on suppression**.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `po_number` | str | Shared across lines of one PO |
| `line_no` | int | Line number within the PO |
| `vendor_id` → `vendor_master.vendor_id` | str | |
| `sku` → `inventory.sku` | str | |
| `quantity` | int | |
| `unit_cost` / `total_cost` | float | |
| `status` | str | `ISSUED` (auto) \| `APPROVED` (post human review) |
| `autonomy_tier` | str | `AUTO_ISSUE` \| `DRAFT_FOR_APPROVAL` |
| `justification` | text \| null | LLM-drafted rationale (draft path) |
| `approved_by` | str \| null | Human approver (draft path) |
| `created_at` | datetime | |

## `notifications`
A message rendered to a Demand Planner (no real email/Slack is sent). The
**Notification Agent** uses the LLM only to phrase `body`.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `recipient` | str | DP slack handle / email |
| `channel` | str | `slack` \| `email` |
| `subject` | str | |
| `body` | text | LLM-phrased message |
| `sku` | str \| null | |
| `po_number` | str \| null | |
| `autonomy_tier` | str \| null | |
| `status` | str | `SENT` \| `PENDING` |
| `created_at` | datetime | |

## `audit_log`  *(immutable, append-only)*
Record of every routing decision, PO, and suppression. Enforced append-only by SQLite
triggers (`audit_log_no_update`, `audit_log_no_delete`) installed in
[`data/database.py`](../data/database.py): UPDATE/DELETE raise `ABORT`.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `run_id` | str \| null | Pipeline run correlation id |
| `created_at` | datetime | |
| `agent` | str | Emitting agent |
| `event_type` | str | `ROUTING_DECISION` \| `PO_WRITTEN` \| `SUPPRESSED` \| `DRAFT_CREATED` \| `NOTIFIED` \| … |
| `sku` | str \| null | |
| `vendor_id` | str \| null | |
| `autonomy_tier` | str \| null | |
| `po_number` | str \| null | |
| `summary` | str | One-line human summary |
| `details` | text \| null | JSON of the deterministic decision inputs |

## `user_directory`
Demand Planners the **Notification Agent** can target — one per sub-category.

| Column | Type | Notes |
|--------|------|-------|
| `id` (PK) | int | Autoincrement |
| `name` | str | |
| `role` | str | Default `Demand Planner` |
| `subcategory` | str | Maps a SKU's `category` to its owner |
| `slack_handle` | str | |
| `email` | str | |

---

## Seeded volumes (from a clean `seed_data.py`)

- **14 vendors** (8 named for scenarios + 6 generic approved pool).
- **51 SKUs** — 11 scenario anchors (`SKU-123`, `SKU-456`, `SKU-701..708`, `SKU-212`) +
  40 healthy background SKUs above threshold.
- **90 days** of `sales_offtake` per SKU.
- **8 season-index** rows, **8 Demand Planners**, **1 open PO** (S4).
- `blue_yonder_po`, `notifications`, `audit_log` start **empty** — they are populated by
  the agents in later phases.
