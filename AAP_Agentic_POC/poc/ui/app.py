"""Streamlit Demand-Planner dashboard — the Phase-5 demo surface.

Run from the ``poc/`` source root::

    streamlit run ui/app.py

What it shows, top to bottom:

* **Run 6 AM scan** — re-seeds the warehouse and runs the Stock Monitor across
  every SKU, listing what it found (low-stock / critical / suppressed) with a
  scenario filter (S1–S4).
* **Worklist** — one card per pipeline result: stock, forecast + basis, vendor +
  status, and a colour-coded route badge (AUTO-ISSUED / NEEDS APPROVAL /
  SUPPRESSED). Draft cards carry the LLM justification, the 90-day offtake chart,
  and Approve / Reject buttons that resume the graph.
* **Blue Yonder (mock)** — the ``blue_yonder_po`` table, read live, so a PO write
  appears the instant an auto-issue or an approval happens.
* **Notifications** and **Audit trail** tabs.

All run/approve/reject logic lives in :mod:`ui.service` (over the orchestration
runner); all reads go through :mod:`ui.data_access` (plain SELECTs). This module
is presentation only. State is held in ``st.session_state`` — server-side, no
browser storage — so the worklist and its decisions survive Streamlit's reruns.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- Make the poc/ source root importable when launched by `streamlit run` ---
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

import streamlit as st  # noqa: E402

import config  # noqa: E402
from ui import data_access as dao  # noqa: E402
from ui import service  # noqa: E402
from ui.scenarios import SCENARIOS  # noqa: E402

# ---------------------------------------------------------------------------
# Page config + badge styling
# ---------------------------------------------------------------------------
st.set_page_config(page_title="DP Replenishment Console", page_icon="📦", layout="wide")

#: Badge colours per route — green=acted automatically, amber=needs a human,
#: grey=suppressed (a false alarm avoided).
_BADGE_CSS = {
    "AUTO-ISSUED": ("#1b5e20", "#c8e6c9"),
    "NEEDS APPROVAL": ("#8d6e00", "#fff3c4"),
    "SUPPRESSED": ("#455a64", "#cfd8dc"),
    "—": ("#333", "#eee"),
}


def badge(label: str) -> str:
    """Render a route label as an inline coloured HTML badge.

    Args:
        label: The badge text (AUTO-ISSUED / NEEDS APPROVAL / SUPPRESSED).

    Returns:
        An HTML ``<span>`` string for ``st.markdown(..., unsafe_allow_html=True)``.

    Rationale: the colour-coded badge is how a stakeholder reads each card's
        outcome at a glance — the demo's most-scanned signal.
    """
    fg, bg = _BADGE_CSS.get(label, _BADGE_CSS["—"])
    return (
        f"<span style='background:{bg};color:{fg};padding:3px 10px;border-radius:12px;"
        f"font-weight:700;font-size:0.80rem;white-space:nowrap'>{label}</span>"
    )


# ---------------------------------------------------------------------------
# Session-state bootstrap
# ---------------------------------------------------------------------------
def _init_state() -> None:
    """Initialise the server-side session keys on first load.

    Rationale: the scan result and decisions live in ``st.session_state`` so they
        persist across Streamlit reruns without any client-side storage.
    """
    st.session_state.setdefault("scan", None)          # ScanResult | None
    st.session_state.setdefault("scan_count", 0)        # monotonic scan id
    st.session_state.setdefault("filter", "All")        # scenario filter
    st.session_state.setdefault("approver", "@demand.planner")
    st.session_state.setdefault("use_llm", config.USE_LLM)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def _sidebar() -> None:
    """Render the demo controls: the scan button, filter, LLM toggle, identity.

    Rationale: the scan is the demo's single primary action; the rest are framing
        controls a presenter touches occasionally.
    """
    with st.sidebar:
        st.header("Demand Planner console")
        st.caption(
            "Mock Palantir + Snowflake/AS400 → six-agent pipeline → mock Blue Yonder. "
            "Dummy data; no real systems."
        )

        if st.button("▶  Run 6 AM scan", type="primary", use_container_width=True):
            _do_scan()

        st.session_state["filter"] = st.radio(
            "Scenario filter",
            options=["All", *[sc.id for sc in SCENARIOS]],
            format_func=lambda x: x if x == "All"
            else f"{x} · {next(s.title for s in SCENARIOS if s.id == x)}",
            horizontal=False,
        )

        st.divider()
        st.session_state["approver"] = st.text_input(
            "Acting as (approver handle)", value=st.session_state["approver"]
        )
        use_llm = st.toggle(
            "Use LLM for narrative / notifications",
            value=st.session_state["use_llm"],
            help="On: Gemini → Ollama → template. Off: deterministic template only. "
            "Either way, routing stays deterministic.",
        )
        if use_llm != st.session_state["use_llm"]:
            st.session_state["use_llm"] = use_llm
            service.set_use_llm(use_llm)

        st.divider()
        st.caption("Scenarios")
        for sc in SCENARIOS:
            st.caption(f"**{sc.id} · {sc.title}** — {sc.summary}")


def _do_scan() -> None:
    """Execute a fresh scan and stash the result in session state.

    Rationale: bumps the scan counter (so run ids stay unique across scans) and
        runs the whole morning pipeline behind a spinner.
    """
    service.set_use_llm(st.session_state["use_llm"])
    st.session_state["scan_count"] += 1
    scan_id = st.session_state["scan_count"]
    with st.spinner("Scanning warehouse and routing every breach…"):
        st.session_state["scan"] = service.run_scan(scan_id)


# ---------------------------------------------------------------------------
# Header / detection
# ---------------------------------------------------------------------------
def _header() -> None:
    """Render the title and the whole-warehouse framing metrics.

    Rationale: anchors the demo — the four flagged SKUs sit against a full
        warehouse of healthy stock the monitor deliberately ignored.
    """
    st.title("📦 Inventory Replenishment — DP Console")
    scan = st.session_state["scan"]
    overview = dao.inventory_overview()
    by = dao.blue_yonder_summary()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs in warehouse", overview["skus"])
    c2.metric("Flagged this scan", len(scan.detection) if scan else 0)
    c3.metric("Blue Yonder POs", by["pos"])
    c4.metric("PO value written", f"${by['value']:,.0f}")


def _detection_section(scan: service.ScanResult) -> None:
    """Render the 6 AM scan's detection list with the signal breakdown.

    Args:
        scan: The current scan result.

    Rationale: shows what the Stock Monitor found before any routing — the
        "what woke us up this morning" view, filterable by scenario.
    """
    counts = scan.signal_counts()
    st.subheader("🔎 6 AM scan — what the Stock Monitor found")
    cols = st.columns(4)
    cols[0].metric("Flagged", len(scan.detection))
    cols[1].metric("Critical-low", counts.get("CRITICAL-LOW", 0))
    cols[2].metric("Low-stock", counts.get("LOW-STOCK", 0))
    cols[3].metric("Suppress candidates", counts.get("SUPPRESS", 0))

    items = _apply_filter(scan.detection, key=lambda d: d.scenario)
    if not items:
        st.info("No flagged SKUs for this scenario filter.")
        return
    st.dataframe(
        [
            {
                "Scenario": d.scenario,
                "SKU": d.sku,
                "Description": d.description,
                "Signal": d.signal,
                "On hand": d.on_hand,
                "Threshold": d.reorder_threshold,
                "Effective": d.effective_stock,
                "Why": d.note,
            }
            for d in items
        ],
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Worklist cards
# ---------------------------------------------------------------------------
def _worklist_section(scan: service.ScanResult) -> None:
    """Render the worklist as one card per pipeline result.

    Args:
        scan: The current scan result (cards held in session state).

    Rationale: the worklist is the heart of the demo — each card pairs the
        deterministic verdict with its colour-coded route and, for drafts, the
        approval surface that resumes the graph.
    """
    st.subheader("🗂️ Worklist")
    cards = _apply_filter(scan.cards, key=lambda c: c.scenario)
    if not cards:
        st.info("No worklist items for this scenario filter.")
        return
    for idx, card in enumerate(cards):
        _render_card(idx, card)


def _render_card(idx: int, card: service.WorklistCard) -> None:
    """Render a single worklist card by kind.

    Args:
        idx: The card's index in the filtered list (used for widget keys).
        card: The card to render.

    Rationale: a thin dispatcher keeps each card kind's layout self-contained.
    """
    with st.container(border=True):
        top, badge_col = st.columns([4, 1])
        top.markdown(f"**{card.scenario} · {card.title}**")
        badge_col.markdown(badge(card.route_label), unsafe_allow_html=True)

        if card.kind == "consolidated":
            _render_consolidated(card)
        elif card.kind == "suppress":
            _render_suppress(card)
        elif card.kind == "draft":
            _render_draft(idx, card)
        else:
            _render_auto(card)


def _render_auto(card: service.WorklistCard) -> None:
    """Render an auto-issued single-SKU card.

    Args:
        card: The auto card.

    Rationale: the happy path — shows the stock breach, the sized order, the
        approved vendor, and the PO that was placed without a human.
    """
    c1, c2, c3 = st.columns(3)
    c1.metric("On hand / threshold", f"{card.on_hand} / {card.reorder_threshold}", card.signal)
    c2.metric("Forecast order qty", card.forecast_qty)
    c3.metric("Vendor", card.vendor_id, f"{card.vendor_status} · {card.approval_status}")
    if card.forecast_basis:
        st.caption(f"Forecast basis — {card.forecast_basis}")
    if card.po_written:
        st.success(
            f"✅ Auto-issued **{card.po_number}** ({card.po_status}) — "
            f"${card.total_cost:,.2f}. Written to Blue Yonder; DP notified."
        )


def _render_consolidated(card: service.WorklistCard) -> None:
    """Render a consolidated multi-SKU PO card (the S3 efficiency win).

    Args:
        card: The consolidated card.

    Rationale: makes "one order per vendor" tangible — the shared PO and its line
        breakdown, which is the whole point of batch consolidation.
    """
    c1, c2, c3 = st.columns(3)
    c1.metric("Vendor", card.vendor_id, "APPROVED")
    c2.metric("SKUs on PO", len(card.line_skus))
    c3.metric("PO value", f"${(card.total_cost or 0):,.2f}")
    if card.po_number:
        st.success(f"✅ Consolidated PO **{card.po_number}** covering {', '.join(card.line_skus)}.")
        lines = dao.po_lines_for(card.po_number)
        if not lines.empty:
            st.dataframe(lines, use_container_width=True, hide_index=True)


def _render_suppress(card: service.WorklistCard) -> None:
    """Render a suppressed card with the in-transit reason.

    Args:
        card: The suppress card.

    Rationale: the false-alarm case — shows why NO PO was the correct action, with
        the in-transit PO that already covers the threshold cited explicitly.
    """
    c1, c2, c3 = st.columns(3)
    c1.metric("On hand / threshold", f"{card.on_hand} / {card.reorder_threshold}", card.signal)
    c2.metric("Effective stock", card.effective_stock)
    c3.metric(
        "Weeks of cover",
        f"{card.weeks_of_cover:.1f}" if card.weeks_of_cover is not None else "—",
    )
    inbound = dao.open_pos_for(card.skus[0]) if card.skus else None
    if inbound is not None and not inbound.empty:
        row = inbound.iloc[0]
        st.warning(
            f"🛑 Suppressed — no new PO. In-transit **{row['po_number']}** "
            f"({int(row['quantity'])} units, {row['status']}, ETA {int(row['eta_days'])}d) "
            f"already lifts effective stock to {card.effective_stock} ≥ {card.reorder_threshold}. "
            "Advisory + audit only."
        )


def _render_draft(idx: int, card: service.WorklistCard) -> None:
    """Render a draft card: justification, 90-day chart, and approve/reject.

    Args:
        idx: Card index (for unique widget keys).
        card: The draft card.

    Rationale: the human-in-the-loop surface — the planner reads the LLM rationale
        and the demand history, then Approve resumes the graph (PO written) or
        Reject leaves Blue Yonder untouched and notifies procurement.
    """
    c1, c2, c3 = st.columns(3)
    c1.metric("On hand / threshold", f"{card.on_hand} / {card.reorder_threshold}", card.signal)
    c2.metric("Forecast order qty", card.forecast_qty)
    c3.metric(
        "Vendor",
        card.vendor_id,
        f"{card.vendor_status} · {card.approval_status}",
    )
    if card.is_critical and card.approval_deadline:
        st.error(f"⚠️ CRITICAL-LOW — SLA: action by {card.approval_deadline}.")
    st.markdown(f"**Why this needs a human:** {card.draft_reason}")

    if card.draft_narrative:
        st.info(card.draft_narrative)

    if card.chart_sku:
        series = dao.offtake_series(card.chart_sku)
        if not series.empty:
            st.caption("Last 90 days offtake (units/day)")
            st.area_chart(series, height=140)

    if card.decided:
        if card.decision_label == "Approved":
            st.success(
                f"✅ Approved — PO **{card.po_number}** ({card.po_status}) written to Blue Yonder."
            )
        else:
            st.warning("🚫 Rejected — no PO written; procurement notified for alternate sourcing.")
        return

    _render_decision_controls(idx, card)


def _render_decision_controls(idx: int, card: service.WorklistCard) -> None:
    """Render the Approve / Reject controls for an undecided draft card.

    Args:
        idx: Card index (for unique widget keys).
        card: The undecided draft card.

    Rationale: the actual resume verbs — kept in one place so both buttons share
        the approver identity and the post-action rerun.
    """
    note = st.text_input("Approval note (optional)", key=f"note-{idx}", value="")
    reason = st.text_input(
        "Rejection reason (required to reject)", key=f"reason-{idx}", value=""
    )
    approver = st.session_state["approver"]
    bc1, bc2, _ = st.columns([1, 1, 3])
    if bc1.button("✅ Approve", key=f"approve-{idx}", type="primary"):
        updated = service.approve_card(card, approver=approver, note=note or None)
        _replace_card(card, updated)
        st.rerun()
    if bc2.button("🚫 Reject", key=f"reject-{idx}"):
        if not reason.strip():
            st.warning("A rejection reason is required.")
        else:
            updated = service.reject_card(card, reason=reason, rejecter=approver)
            _replace_card(card, updated)
            st.rerun()


def _replace_card(old: service.WorklistCard, new: service.WorklistCard) -> None:
    """Swap a decided card back into the session-state scan result.

    Args:
        old: The card that was acted on.
        new: Its refreshed, decided replacement.

    Rationale: the scan result in session state is the single source of truth the
        UI re-renders, so a decision updates it directly.
    """
    scan = st.session_state["scan"]
    scan.cards = [new if c.run_id == old.run_id and c.scenario == old.scenario else c
                  for c in scan.cards]


# ---------------------------------------------------------------------------
# Right-hand panels: Blue Yonder, notifications, audit
# ---------------------------------------------------------------------------
def _blue_yonder_panel() -> None:
    """Render the live Blue Yonder (mock) PO table.

    Rationale: read live on every rerun so a PO write — auto-issue or approval —
        shows up the instant the table changes, the "watch it happen" effect.
    """
    st.subheader("🏭 Blue Yonder (mock SCPO)")
    summary = dao.blue_yonder_summary()
    c1, c2, c3 = st.columns(3)
    c1.metric("POs", summary["pos"])
    c2.metric("Lines", summary["lines"])
    c3.metric("Value", f"${summary['value']:,.0f}")
    df = dao.blue_yonder_lines()
    if df.empty:
        st.caption("No POs yet — run the scan, then approve a draft to watch rows land here.")
        return
    st.dataframe(
        df[["po_number", "sku", "quantity", "total_cost", "status", "autonomy_tier", "approved_by"]],
        use_container_width=True,
        hide_index=True,
        height=360,
    )


def _notifications_tab() -> None:
    """Render the DP notifications feed.

    Rationale: shows exactly what each planner (or procurement) would have
        received for every routed SKU.
    """
    df = dao.notifications()
    if df.empty:
        st.caption("No notifications yet.")
        return
    for _, row in df.iterrows():
        with st.container(border=True):
            st.markdown(
                f"**{row['subject']}**  \n"
                f"`{row['channel']}` → {row['recipient']}"
                + (f" · PO {row['po_number']}" if row["po_number"] else "")
            )
            st.caption(row["body"])


def _audit_tab() -> None:
    """Render the immutable audit trail.

    Rationale: the evidence surface — every routing decision, PO, and suppression
        appended append-only, shown newest first.
    """
    df = dao.audit_rows()
    if df.empty:
        st.caption("No audit rows yet.")
        return
    st.caption(f"{len(df)} immutable audit rows (append-only; UPDATE/DELETE blocked at the DB).")
    st.dataframe(
        df[["created_at", "agent", "event_type", "sku", "autonomy_tier", "po_number", "summary"]],
        use_container_width=True,
        hide_index=True,
        height=460,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _apply_filter(items: list, key) -> list:
    """Filter a list of cards/detection items by the active scenario filter.

    Args:
        items: The items to filter.
        key: A callable returning each item's scenario id.

    Returns:
        The items whose scenario matches the filter (all of them when 'All').

    Rationale: one filter helper keeps the detection list and the worklist in
        lock-step with the sidebar selection.
    """
    selected = st.session_state["filter"]
    if selected == "All":
        return list(items)
    return [it for it in items if key(it) == selected]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Compose the full dashboard.

    Rationale: a single entry point Streamlit runs top-to-bottom on every
        interaction; all durable state lives in ``st.session_state``.
    """
    _init_state()
    _sidebar()
    _header()

    scan = st.session_state["scan"]
    left, right = st.columns([3, 2], gap="large")
    with left:
        if scan is None:
            st.info("Click **▶ Run 6 AM scan** in the sidebar to start the demo.")
        else:
            _detection_section(scan)
            st.divider()
            _worklist_section(scan)
    with right:
        _blue_yonder_panel()
        st.divider()
        tab_notif, tab_audit = st.tabs(["🔔 Notifications", "🧾 Audit trail"])
        with tab_notif:
            _notifications_tab()
        with tab_audit:
            _audit_tab()


# `streamlit run` executes the module top-level, so just call main().
main()
