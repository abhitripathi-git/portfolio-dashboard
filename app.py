"""Main Streamlit entry point for the multi-user Indian Stock & MF Portfolio Dashboard."""

from __future__ import annotations

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
from ui.data_service import get_enriched_snapshot
from ui.formatters import inject_custom_css


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
        f'<span class="owner-pill">{selection.owner_label}{scope_suffix}</span>'
        f'<span class="live-pill">{eq_cnt} Equities · {mf_cnt} MFs</span>'
        '</div>'
        '</div>'
    )
    st.markdown(hero_html, unsafe_allow_html=True)

    tab_overview, tab_bench, tab_holdings, tab_alt, tab_upload, tab_gemini = st.tabs(
        [
            ":material/dashboard: Overview",
            ":material/monitoring: Benchmarks",
            ":material/account_balance_wallet: Holdings",
            ":material/account_balance: Alt Assets",
            ":material/upload_file: Upload & Data",
            ":material/auto_awesome: Ask Gemini",
        ]
    )

    with tab_overview:
        render_overview_tab(snapshot, selection)

    with tab_bench:
        render_benchmark_tab(selection)

    with tab_holdings:
        render_holdings_tab(snapshot, selection)

    with tab_alt:
        render_alt_assets_tab(snapshot, selection)

    with tab_upload:
        render_upload_tab(selection)

    with tab_gemini:
        render_gemini_tab(selection, snapshot=snapshot)


if __name__ == "__main__":
    main()
