"""Main Streamlit entry point for the multi-user Indian Stock & MF Portfolio Dashboard."""

from __future__ import annotations

import html

import streamlit as st

from core.db import init_db
from ui import (
    render_alt_assets_tab,
    render_benchmark_tab,
    render_gemini_tab,
    render_holdings_tab,
    render_overview_tab,
    render_sidebar,
    render_upload_tab,
)
from ui.data_service import get_data_version, get_enriched_snapshot, trigger_background_warmup
from ui.formatters import inject_custom_css


_PERSISTENT_WIDGET_KEYS = (
    "overview_cap_segment",
    "bench_select",
    "bench_preset",
    "bench_segment_select",
    "bench_mf_select",
    "bench_exclusions",
    "bench_adj_splits",
    "eq_holdings_search",
    "eq_show_details",
    "mf_show_details",
    "tx_ledger_search",
    "tx_side_filter",
    "tx_sort_desc",
    "tx_show_details",
    "movers_metric_radio",
    "movers_scope_radio",
)


def _sync_tab_widget_state(scope_sig: tuple[object, ...]) -> None:
    """Keep tab filter/selection state intact when conditionally rendering only the active tab."""
    if st.session_state.get("_widget_scope_sig") != scope_sig:
        st.session_state["_widget_scope_sig"] = scope_sig
        st.session_state["_saved_tab_widgets"] = {}
        return
    saved = st.session_state.setdefault("_saved_tab_widgets", {})
    for key in _PERSISTENT_WIDGET_KEYS:
        if key in st.session_state:
            saved[key] = st.session_state[key]
        elif key in saved:
            st.session_state[key] = saved[key]


def main() -> None:
    st.set_page_config(
        page_title="Family Portfolio Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_db()
    inject_custom_css()

    selection = render_sidebar()
    _sync_tab_widget_state((selection.owner, selection.account_id))

    with st.spinner("Loading portfolio snapshot..."):
        snapshot = get_enriched_snapshot(
            owner=selection.owner,
            account_id=selection.account_id,
            db_path=selection.db_path,
        )

    totals = snapshot.get("totals", {})
    eq_cnt = totals.get("holdings_count", 0)
    mf_cnt = totals.get("mf_count", 0)
    scope_suffix = (
        f" · {selection.account_label}" if selection.account_id is not None else ""
    )
    hero_html = (
        '<div class="hero-header">'
        '<div>'
        '<div class="hero-title">Family Portfolio Dashboard</div>'
        '<div class="hero-sub">Indian Equities, ETFs &amp; Mutual Funds · On-demand live market analytics</div>'
        '</div>'
        '<div class="hero-badges">'
        f'<span class="owner-pill">{html.escape(selection.owner_label)}{html.escape(scope_suffix)}</span>'
        f'<span class="live-pill">{eq_cnt} Equities · {mf_cnt} MFs</span>'
        '</div>'
        '</div>'
    )
    st.markdown(hero_html, unsafe_allow_html=True)

    # Start warming Benchmarks in a background thread so Overview paints immediately
    trigger_background_warmup(selection.owner, selection.db_path, get_data_version())

    nav_items = [
        ("overview", ":material/dashboard: Overview"),
        ("benchmarks", ":material/monitoring: Benchmarks"),
        ("holdings", ":material/account_balance_wallet: Holdings"),
        ("alt_assets", ":material/account_balance: Alt Assets"),
        ("upload", ":material/upload_file: Upload & Data"),
        ("gemini", ":material/auto_awesome: Ask Gemini"),
    ]
    nav_labels = dict(nav_items)
    nav_keys = [k for k, _ in nav_items]
    if "active_nav_tab" not in st.session_state or st.session_state["active_nav_tab"] not in nav_labels:
        st.session_state["active_nav_tab"] = "overview"

    chosen_tab = st.segmented_control(
        "Navigation",
        options=nav_keys,
        format_func=lambda k: nav_labels.get(k, k),
        default=st.session_state["active_nav_tab"],
        key="main_nav_control",
        label_visibility="collapsed",
    )
    if chosen_tab:
        st.session_state["active_nav_tab"] = chosen_tab
    active_tab = st.session_state["active_nav_tab"]

    if active_tab == "overview":
        render_overview_tab(snapshot, selection)
    elif active_tab == "benchmarks":
        render_benchmark_tab(selection)
    elif active_tab == "holdings":
        render_holdings_tab(snapshot, selection)
    elif active_tab == "alt_assets":
        render_alt_assets_tab(snapshot, selection)
    elif active_tab == "upload":
        render_upload_tab(selection)
    elif active_tab == "gemini":
        render_gemini_tab(selection, snapshot=snapshot)


if __name__ == "__main__":
    main()
