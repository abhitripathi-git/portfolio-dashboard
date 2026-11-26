"""Benchmark & Simulator tab: Portfolio vs. Benchmark with scrip exclusion simulation."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import BENCHMARKS
from core.db import query_holdings, query_mf_holdings, query_transactions
from ui.data_service import (
    cached_benchmark_comparison,
    cached_mf_benchmark_comparison,
    get_data_version,
)
from ui.formatters import (
    COLOR_PALETTE,
    GAIN_COLOR,
    LOSS_COLOR,
    format_inr,
    format_pct,
    format_xirr,
    style_plotly_fig,
)
from ui.sidebar import SidebarSelection


def _resolve_date_window(preset: str, custom_start: date, custom_end: date) -> tuple[str | None, str]:
    today = date.today()
    end_iso = today.isoformat()
    if preset == "1M":
        return (today - timedelta(days=30)).isoformat(), end_iso
    if preset == "3M":
        return (today - timedelta(days=90)).isoformat(), end_iso
    if preset == "6M":
        return (today - timedelta(days=180)).isoformat(), end_iso
    if preset == "YTD":
        return date(today.year, 1, 1).isoformat(), end_iso
    if preset == "1Y":
        return (today - timedelta(days=365)).isoformat(), end_iso
    if preset == "All":
        return None, end_iso
    return custom_start.isoformat(), custom_end.isoformat()


def render_benchmark_tab(selection: SidebarSelection) -> None:
    """Render the Benchmark & Simulator tab."""
    st.subheader("Benchmark Comparison & What-If Scrip Simulator")
    if selection.account_id is not None:
        st.caption(
            f"Note: Benchmark replay runs across all equity accounts for **{selection.owner_label}**."
        )

    db_path = selection.db_path
    eq_holdings = query_holdings(owner=selection.owner, db_path=db_path)
    mf_holdings = query_mf_holdings(owner=selection.owner, db_path=db_path)
    eq_txs = [
        tx
        for tx in query_transactions(owner=selection.owner, db_path=db_path)
        if str(tx.get("asset_type", "EQUITY")).strip().upper() != "MF"
    ]
    all_symbols = sorted(
        {str(h["symbol"]).strip().upper() for h in eq_holdings if h.get("symbol")}
        | {str(tx["symbol"]).strip().upper() for tx in eq_txs if tx.get("symbol")}
    )

    if not all_symbols and not mf_holdings:
        st.info(
            "No holdings or transactions found yet for this owner. "
            "Upload a 5paisa tradebook/holdings file or add manual trades in **Upload & Refresh** "
            "to run benchmark simulations."
        )
        return

    # Controls row
    c1, c2, c3, c4, c5 = st.columns([1.15, 0.8, 1.15, 1.3, 0.85])
    benchmark_keys = list(BENCHMARKS.keys())
    default_idx = benchmark_keys.index("NIFTY_MIDCAP_150") if "NIFTY_MIDCAP_150" in benchmark_keys else 0

    # Build dynamic segment options (Region + Advisory Sources)
    regions_present = sorted({str(h.get("region") or "India").strip() for h in eq_holdings} or {"India"})
    advisors_present = sorted({str(h.get("advisory_source") or "Self").strip() for h in eq_holdings} or {"Self"})
    segment_options: list[tuple[str, str, str | None, str | None]] = [
        ("NONE", "Overall Combined Only", None, None),
    ]
    for reg in regions_present:
        segment_options.append((f"REGION:{reg}", f"Region: {reg}", reg, None))
    for adv in advisors_present:
        segment_options.append((f"ADV:{adv}", f"Advisory: {adv}", None, adv))

    with c1:
        bench_key = st.selectbox(
            "Benchmark Index",
            options=benchmark_keys,
            index=default_idx,
            format_func=lambda k: BENCHMARKS[k]["label"],
            key="bench_select",
        )
    with c2:
        preset = st.selectbox(
            "Date Window",
            options=["1M", "3M", "6M", "YTD", "1Y", "All", "Custom"],
            index=4,
            key="bench_preset",
        )
    with c3:
        default_seg_idx = next(
            (i for i, opt in enumerate(segment_options) if opt[0] == "REGION:India"),
            0,
        )
        selected_segment = st.selectbox(
            "Compare Segment",
            options=segment_options,
            index=default_seg_idx,
            format_func=lambda opt: opt[1],
            key="bench_segment_select",
            help="Overall Combined (blue line) is always shown. Pick a region or advisory source to overlay its line.",
        )
    with c4:
        excluded_scrips = st.multiselect(
            "Exclude Scrips (What-If Simulator)",
            options=all_symbols,
            default=[],
            placeholder="Pick stocks to exclude (e.g. top 5)...",
            key="bench_exclude_scrips",
        )
    with c5:
        chart_mode = st.radio(
            "Scale",
            options=["Base 100", "₹ Value"],
            horizontal=True,
            key="bench_chart_mode",
        )

    today = date.today()
    custom_start = today - timedelta(days=365)
    custom_end = today
    if preset == "Custom":
        dc1, dc2, _ = st.columns([1, 1, 2])
        with dc1:
            custom_start = st.date_input(
                "Start Date",
                value=today - timedelta(days=180),
                max_value=today,
                key="bench_custom_start",
            )
        with dc2:
            custom_end = st.date_input(
                "End Date",
                value=today,
                max_value=today,
                key="bench_custom_end",
            )

    adjust_splits = st.checkbox(
        "Adjust tradebook quantities for corporate stock splits",
        value=True,
        key="bench_adjust_splits",
    )

    start_iso, end_iso = _resolve_date_window(preset, custom_start, custom_end)
    seg_code, seg_label, seg_region, seg_adv = selected_segment

    with st.spinner("Computing portfolio replay & fetching benchmark prices..."):
        res = cached_benchmark_comparison(
            owner=selection.owner,
            benchmark_key=bench_key,
            start_date=start_iso,
            end_date=end_iso,
            exclude_symbols=tuple(sorted(excluded_scrips)),
            adjust_for_splits=adjust_splits,
            db_path_str=str(db_path),
            version=get_data_version(),
        )
        seg_res: dict[str, Any] | None = None
        if seg_code != "NONE":
            seg_res = cached_benchmark_comparison(
                owner=selection.owner,
                benchmark_key=bench_key,
                start_date=start_iso,
                end_date=end_iso,
                exclude_symbols=tuple(sorted(excluded_scrips)),
                adjust_for_splits=adjust_splits,
                db_path_str=str(db_path),
                version=get_data_version(),
                region=seg_region,
                advisory_source=seg_adv,
            )

    summary: dict[str, Any] = res.get("summary", {})
    ts_rows: list[dict[str, Any]] = res.get("timeseries", [])
    warnings: list[str] = res.get("warnings", [])
    seg_summary: dict[str, Any] = (seg_res or {}).get("summary", {})
    seg_ts_rows: list[dict[str, Any]] = (seg_res or {}).get("timeseries", [])

    if excluded_scrips:
        st.caption(
            f"Simulating portfolio **excluding {len(excluded_scrips)} scrip(s)**: "
            f"`{', '.join(excluded_scrips)}` · Remaining active scrips: **{len(res.get('included_symbols', []))}**"
        )

    # KPI Summary Cards
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.metric(
            "Portfolio TWR",
            format_pct(summary.get("portfolio_twr_pct")),
            delta=f"Simple: {format_pct(summary.get('portfolio_simple_return_pct'))}",
            delta_color="off",
        )
    with m2:
        st.metric(
            "Portfolio XIRR",
            format_xirr(summary.get("portfolio_xirr")),
            delta=f"Mode: {summary.get('mode', '—')}",
            delta_color="off",
        )
    with m3:
        st.metric(
            f"{summary.get('benchmark_label', bench_key)}",
            format_pct(summary.get("benchmark_twr_pct")),
            delta=f"XIRR: {format_xirr(summary.get('benchmark_xirr'))}",
            delta_color="off",
        )
    with m4:
        alpha_twr = summary.get("alpha_twr_pct")
        st.metric(
            "Alpha (TWR)",
            format_pct(alpha_twr),
            delta="vs Benchmark",
            delta_color="off",
        )
    with m5:
        alpha_xirr = summary.get("alpha_xirr_pct")
        st.metric(
            "Alpha (XIRR)",
            format_pct(alpha_xirr),
            delta="Annualized",
            delta_color="off",
        )
    with m6:
        st.metric(
            "End Value vs Matched",
            format_inr(summary.get("portfolio_end_value")),
            delta=f"Bench: {format_inr(summary.get('benchmark_matched_value'))}",
            delta_color="off",
        )

    if warnings:
        with st.expander(f"Simulation Notes ({len(warnings)})", expanded=False):
            for w in warnings:
                st.caption(f"• {w}")

    if not ts_rows:
        st.warning("No equity timeseries points returned for the selected date window and symbols.")
    else:
        df = pd.DataFrame(ts_rows)
        df["date"] = pd.to_datetime(df["date"])
        seg_df = pd.DataFrame(seg_ts_rows) if seg_ts_rows else pd.DataFrame()
        if not seg_df.empty:
            seg_df["date"] = pd.to_datetime(seg_df["date"])

        fig = go.Figure()
        if chart_mode == "Base 100":
            fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=df["portfolio_index"],
                    mode="lines",
                    name=f"Overall Combined ({summary.get('owner', 'Combined')})",
                    line=dict(color=COLOR_PALETTE[0], width=2.8),
                    customdata=df["portfolio_return_pct"],
                    hovertemplate="Overall Combined: %{y:.2f} (%{customdata:+.2f}%)<extra></extra>",
                )
            )
            if not seg_df.empty:
                fig.add_trace(
                    go.Scatter(
                        x=seg_df["date"],
                        y=seg_df["portfolio_index"],
                        mode="lines",
                        name=seg_label,
                        line=dict(color=COLOR_PALETTE[1], width=2.5),
                        customdata=seg_df["portfolio_return_pct"],
                        hovertemplate=f"{seg_label}: " + "%{y:.2f} (%{customdata:+.2f}%)<extra></extra>",
                    )
                )
            if df["benchmark_index"].notna().any():
                fig.add_trace(
                    go.Scatter(
                        x=df["date"],
                        y=df["benchmark_index"],
                        mode="lines",
                        name=summary.get("benchmark_label", bench_key),
                        line=dict(color=COLOR_PALETTE[2], width=2.2, dash="dot"),
                        customdata=df["benchmark_return_pct"],
                        hovertemplate=f"{summary.get('benchmark_label', bench_key)}: "
                        "%{y:.2f} (%{customdata:+.2f}%)<extra></extra>",
                    )
                )
            fig.add_hline(y=100.0, line_dash="dash", line_color="rgba(148,163,184,0.45)")
            style_plotly_fig(
                fig,
                title=f"Normalized Performance (Base 100) · {summary.get('start_date')} to {summary.get('end_date')}",
                height=430,
            )
            fig.update_yaxes(title_text="Index (Base = 100)")
        else:
            fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=df["portfolio_value"],
                    mode="lines",
                    name="Overall Combined (₹)",
                    line=dict(color=COLOR_PALETTE[0], width=2.8),
                    hovertemplate="Overall Combined: ₹%{y:,.0f}<extra></extra>",
                )
            )
            if not seg_df.empty:
                fig.add_trace(
                    go.Scatter(
                        x=seg_df["date"],
                        y=seg_df["portfolio_value"],
                        mode="lines",
                        name=f"{seg_label} (₹)",
                        line=dict(color=COLOR_PALETTE[1], width=2.5),
                        hovertemplate=f"{seg_label}: ₹" + "%{y:,.0f}<extra></extra>",
                    )
                )
            if df["benchmark_value"].notna().any():
                fig.add_trace(
                    go.Scatter(
                        x=df["date"],
                        y=df["benchmark_value"],
                        mode="lines",
                        name=f"Matched {summary.get('benchmark_label', bench_key)} (₹)",
                        line=dict(color=COLOR_PALETTE[2], width=2.2, dash="dot"),
                        hovertemplate="Benchmark Matched: ₹%{y:,.0f}<extra></extra>",
                    )
                )
            fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=df["net_invested"],
                    mode="lines",
                    name="Net Invested (₹)",
                    line=dict(color="#94A3B8", width=1.6, dash="dash"),
                    hovertemplate="Net Invested: ₹%{y:,.0f}<extra></extra>",
                )
            )
            style_plotly_fig(
                fig,
                title=f"Portfolio Value vs. Cashflow-Matched Benchmark (₹) · {summary.get('start_date')} to {summary.get('end_date')}",
                height=430,
            )
            fig.update_yaxes(title_text="Value (₹)")

        st.plotly_chart(fig, width="stretch")

        # Quick comparison table
        with st.expander("Detailed Simulation Metrics & Included Scrips", expanded=not seg_df.empty):
            comp_rows = [
                {
                    "Metric": "Time-Weighted Return (TWR)",
                    "Overall Combined": format_pct(summary.get("portfolio_twr_pct")),
                    **({seg_label: format_pct(seg_summary.get("portfolio_twr_pct"))} if not seg_df.empty else {}),
                    "Benchmark": format_pct(summary.get("benchmark_twr_pct")),
                    "Overall Alpha": format_pct(summary.get("alpha_twr_pct")),
                },
                {
                    "Metric": "Annualized XIRR",
                    "Overall Combined": format_xirr(summary.get("portfolio_xirr")),
                    **({seg_label: format_xirr(seg_summary.get("portfolio_xirr"))} if not seg_df.empty else {}),
                    "Benchmark": format_xirr(summary.get("benchmark_xirr")),
                    "Overall Alpha": format_pct(summary.get("alpha_xirr_pct")),
                },
                {
                    "Metric": "Simple Return",
                    "Overall Combined": format_pct(summary.get("portfolio_simple_return_pct")),
                    **({seg_label: format_pct(seg_summary.get("portfolio_simple_return_pct"))} if not seg_df.empty else {}),
                    "Benchmark": format_pct(summary.get("benchmark_simple_return_pct")),
                    "Overall Alpha": "—",
                },
                {
                    "Metric": "Ending Value",
                    "Overall Combined": format_inr(summary.get("portfolio_end_value")),
                    **({seg_label: format_inr(seg_summary.get("portfolio_end_value"))} if not seg_df.empty else {}),
                    "Benchmark": format_inr(summary.get("benchmark_matched_value")),
                    "Overall Alpha": format_inr(
                        (summary.get("portfolio_end_value") or 0)
                        - (summary.get("benchmark_matched_value") or 0),
                        signed=True,
                    )
                    if summary.get("benchmark_matched_value") is not None
                    else "—",
                },
            ]
            st.dataframe(pd.DataFrame(comp_rows), width="stretch", hide_index=True)
            st.caption(f"Overall included symbols ({len(res.get('included_symbols', []))}): {', '.join(res.get('included_symbols', []))}")
            if seg_res is not None and seg_res.get("included_symbols"):
                st.caption(f"{seg_label} symbols ({len(seg_res.get('included_symbols', []))}): {', '.join(seg_res.get('included_symbols', []))}")
            if res.get("excluded_symbols"):
                st.caption(f"Excluded symbols: {', '.join(res['excluded_symbols'])}")

    # Mutual Funds vs Benchmark Section
    if mf_holdings:
        st.divider()
        st.subheader("Mutual Funds vs. Benchmark")
        mf_options: list[tuple[str, str]] = [("ALL", "All Mutual Funds (Combined)")]
        seen_mf: set[str] = set()
        for m in mf_holdings:
            code = str(m.get("amfi_code", "")).strip()
            sname = str(m.get("scheme_name", "")).strip()
            key = code or sname
            if key and key not in seen_mf:
                seen_mf.add(key)
                label = f"{sname} ({code})" if code else sname
                mf_options.append((key, label))
        mf_options.append(("NONE", "Hide Mutual Fund Comparison"))

        mf_choice = st.selectbox(
            "Select Mutual Fund",
            options=mf_options,
            index=0,
            format_func=lambda item: item[1],
            key="bench_mf_select",
        )
        mf_key, mf_label = mf_choice
        if mf_key != "NONE":
            with st.spinner("Fetching AMFI historical NAVs & computing MF benchmark comparison..."):
                mf_res = cached_mf_benchmark_comparison(
                    owner=selection.owner,
                    benchmark_key=bench_key,
                    start_date=start_iso,
                    end_date=end_iso,
                    scheme_filter=mf_key,
                    db_path_str=str(db_path),
                    version=get_data_version(),
                )
            mf_sum = mf_res.get("summary", {})
            mf_ts = mf_res.get("timeseries", [])
            mf_warns = mf_res.get("warnings", [])

            mc1, mc2, mc3, mc4, mc5 = st.columns(5)
            with mc1:
                st.metric("MF Return (TWR)", format_pct(mf_sum.get("portfolio_twr_pct")))
            with mc2:
                st.metric("MF XIRR", format_xirr(mf_sum.get("portfolio_xirr")))
            with mc3:
                st.metric(
                    f"{mf_sum.get('benchmark_label', bench_key)}",
                    format_pct(mf_sum.get("benchmark_twr_pct")),
                    delta=f"XIRR: {format_xirr(mf_sum.get('benchmark_xirr'))}",
                    delta_color="off",
                )
            with mc4:
                st.metric("MF Alpha (TWR)", format_pct(mf_sum.get("alpha_twr_pct")))
            with mc5:
                st.metric(
                    "MF End Value",
                    format_inr(mf_sum.get("portfolio_end_value")),
                    delta=f"Bench: {format_inr(mf_sum.get('benchmark_matched_value'))}",
                    delta_color="off",
                )

            if mf_warns:
                for w in mf_warns:
                    st.caption(f"• {w}")

            if mf_ts:
                mf_df = pd.DataFrame(mf_ts)
                mf_df["date"] = pd.to_datetime(mf_df["date"])
                fig_mf = go.Figure()
                if chart_mode == "Base 100":
                    fig_mf.add_trace(
                        go.Scatter(
                            x=mf_df["date"],
                            y=mf_df["portfolio_index"],
                            mode="lines",
                            name=mf_label,
                            line=dict(color=COLOR_PALETTE[3], width=2.8),
                            customdata=mf_df["portfolio_return_pct"],
                            hovertemplate=f"{mf_label}: " + "%{y:.2f} (%{customdata:+.2f}%)<extra></extra>",
                        )
                    )
                    if mf_df["benchmark_index"].notna().any():
                        fig_mf.add_trace(
                            go.Scatter(
                                x=mf_df["date"],
                                y=mf_df["benchmark_index"],
                                mode="lines",
                                name=mf_sum.get("benchmark_label", bench_key),
                                line=dict(color=COLOR_PALETTE[2], width=2.2, dash="dot"),
                                customdata=mf_df["benchmark_return_pct"],
                                hovertemplate=f"{mf_sum.get('benchmark_label', bench_key)}: "
                                "%{y:.2f} (%{customdata:+.2f}%)<extra></extra>",
                            )
                        )
                    fig_mf.add_hline(y=100.0, line_dash="dash", line_color="rgba(148,163,184,0.45)")
                    style_plotly_fig(
                        fig_mf,
                        title=f"Mutual Fund Performance (Base 100) · {mf_sum.get('start_date')} to {mf_sum.get('end_date')}",
                        height=380,
                    )
                    fig_mf.update_yaxes(title_text="Index (Base = 100)")
                else:
                    fig_mf.add_trace(
                        go.Scatter(
                            x=mf_df["date"],
                            y=mf_df["portfolio_value"],
                            mode="lines",
                            name=f"{mf_label} (₹)",
                            line=dict(color=COLOR_PALETTE[3], width=2.8),
                            hovertemplate=f"{mf_label}: ₹" + "%{y:,.0f}<extra></extra>",
                        )
                    )
                    if mf_df["benchmark_value"].notna().any():
                        fig_mf.add_trace(
                            go.Scatter(
                                x=mf_df["date"],
                                y=mf_df["benchmark_value"],
                                mode="lines",
                                name=f"Matched {mf_sum.get('benchmark_label', bench_key)} (₹)",
                                line=dict(color=COLOR_PALETTE[2], width=2.2, dash="dot"),
                                hovertemplate="Benchmark Matched: ₹%{y:,.0f}<extra></extra>",
                            )
                        )
                    style_plotly_fig(
                        fig_mf,
                        title=f"Mutual Fund Value vs. Matched Benchmark (₹) · {mf_sum.get('start_date')} to {mf_sum.get('end_date')}",
                        height=380,
                    )
                    fig_mf.update_yaxes(title_text="Value (₹)")
                st.plotly_chart(fig_mf, width="stretch")
            else:
                st.info("No historical NAV points found for the selected mutual fund and date range.")
