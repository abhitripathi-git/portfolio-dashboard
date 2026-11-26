"""Overview tab: symmetrical KPI grid, allocation visuals, advisory & geography breakdowns, and tech status."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.analytics import compute_cap_distribution
from core.db import (
    query_cap_allocations,
    query_holding_baselines,
    query_other_assets,
    query_transactions,
)
from core.gemini_chat import fetch_cap_allocations_via_gemini, load_env_api_key
from ui.formatters import (
    COLOR_PALETTE,
    format_inr,
    format_inr_compact,
    format_pct,
    format_xirr,
    style_plotly_fig,
)
from ui.sidebar import SidebarSelection


def render_empty_onboarding(selection: SidebarSelection) -> None:
    """Display clean onboarding instructions when no data is imported yet."""
    st.info(
        f"No holdings or transactions found for **{selection.owner_label}** (`{selection.db_path.name}`)."
    )
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            "#### 1. Upload Broker & CAS Files\n"
            "Go to **Upload & Refresh** to import:\n"
            "- **5paisa** tradebook or holdings (`.csv`, `.xlsx`, `.pdf`)\n"
            "- **CAMS / KFintech CAS** statement (`.pdf`, `.xlsx`, `.csv`)"
        )
    with col2:
        st.markdown(
            "#### 2. Quick Monthly Delta\n"
            "Paste one-line trades in **Upload & Refresh**:\n"
            "```text\n"
            "2026-11-20 BUY RELIANCE 10 @ 2950\n"
            "2026-11-21 SELL TCS 5 @ 4100\n"
            "```"
        )


def _kpi_card_html(label: str, value: str, sub_text: str, tone: str = "neu") -> str:
    return (
        '<div class="kpi-card">'
        f'<div class="kpi-label">{html.escape(label)}</div>'
        f'<div class="kpi-value">{html.escape(value)}</div>'
        f'<div class="kpi-sub"><span class="kpi-pill {tone}">{html.escape(sub_text)}</span></div>'
        '</div>'
    )


def _build_group_summary_df(
    holdings: list[dict[str, Any]],
    field: str,
    default_val: str,
) -> pd.DataFrame:
    groups: dict[str, dict[str, float]] = {}
    total_curr = sum(float(h.get("current_value", 0.0)) for h in holdings)
    for h in holdings:
        key_val = str(h.get(field) or default_val).strip() or default_val
        grp = groups.setdefault(key_val, {"Positions": 0, "Invested": 0.0, "Current Value": 0.0})
        grp["Positions"] += 1
        grp["Invested"] += float(h.get("invested_value", 0.0))
        grp["Current Value"] += float(h.get("current_value", 0.0))
    rows = []
    for label, vals in sorted(groups.items(), key=lambda x: x[1]["Current Value"], reverse=True):
        inv, cur = vals["Invested"], vals["Current Value"]
        pnl = cur - inv
        rows.append(
            {
                field: label,
                "positions": int(vals["Positions"]),
                "invested_value": round(inv, 2),
                "current_value": round(cur, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round((pnl / inv * 100.0) if inv > 0 else 0.0, 2),
                "weight_pct": round((cur / total_curr * 100.0) if total_curr > 0 else 0.0, 2),
            }
        )
    return pd.DataFrame(rows)


def render_overview_tab(snapshot: dict[str, Any], selection: SidebarSelection) -> None:
    """Render the Overview tab."""
    totals = snapshot.get("totals", {})
    holdings = snapshot.get("holdings", [])
    db_path: Path = selection.db_path

    if not holdings:
        recent_txs = query_transactions(
            owner=selection.owner, account_id=selection.account_id, db_path=db_path
        )
        if not recent_txs:
            render_empty_onboarding(selection)
            return

    if snapshot.get("warnings"):
        with st.expander(f"Pricing & Analytics Notes ({len(snapshot['warnings'])})", expanded=False):
            for w in snapshot["warnings"]:
                st.caption(f"• {w}")

    # 1. Uniform Equal-Height 5-Card KPI Grid (Market Portfolio: Equity + ETFs + SIF + MFs)
    other_rows = query_other_assets(owner=selection.owner, db_path=db_path)
    alt_invested = sum(float(r.get("invested_value") or 0.0) for r in other_rows)
    alt_current = sum(float(r.get("current_value") or 0.0) for r in other_rows)

    inv_total = float(totals.get("invested", 0.0))
    cur_total = float(totals.get("current_value", 0.0))
    pnl_val = float(totals.get("pnl", 0.0))
    pnl_pct = float(totals.get("pnl_pct", 0.0))
    day_val = float(totals.get("day_change", 0.0))
    day_pct = float(totals.get("day_change_pct", 0.0))
    xirr_val = totals.get("xirr")
    eq_xirr = totals.get("equity_xirr")

    pnl_tone = "pos" if pnl_val > 0 else ("neg" if pnl_val < 0 else "neu")
    day_tone = "pos" if day_val > 0 else ("neg" if day_val < 0 else "neu")
    xirr_note = f"Eq: {format_xirr(eq_xirr)}" if eq_xirr is not None else "Needs full cashflows"

    kpi_html = (
        '<div class="kpi-grid">'
        + _kpi_card_html(
            "Invested Capital",
            format_inr(inv_total),
            f"{format_inr_compact(inv_total)} cost basis",
            "neu",
        )
        + _kpi_card_html(
            "Current Value",
            format_inr(cur_total),
            f"{totals.get('holdings_count', 0)} Eq · {totals.get('mf_count', 0)} MF",
            "neu",
        )
        + _kpi_card_html(
            "Overall P&L",
            format_inr(pnl_val, signed=True),
            f"{format_pct(pnl_pct)} · {format_inr_compact(pnl_val)}",
            pnl_tone,
        )
        + _kpi_card_html(
            "Day Change (Eq)",
            format_inr(day_val, signed=True),
            f"{format_pct(day_pct)} today",
            day_tone,
        )
        + _kpi_card_html(
            "Portfolio XIRR",
            format_xirr(xirr_val),
            xirr_note,
            "neu",
        )
        + "</div>"
    )
    st.markdown(kpi_html, unsafe_allow_html=True)

    # Subtle Net Worth Roll-Up Strip (full editor lives in the 'Alt Assets' tab)
    if other_rows:
        total_net_worth = cur_total + alt_current
        alt_status = (
            f"Alt & Fixed: {html.escape(format_inr_compact(alt_current))} ({len(other_rows)} items)"
            if alt_current > 0
            else f"Alt & Fixed: ₹0 ({len(other_rows)} tracked · update in Alt Assets tab)"
        )
        nw_html = (
            '<div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;'
            'gap:0.6rem;padding:0.55rem 0.95rem;margin:-0.45rem 0 1.05rem 0;border-radius:10px;'
            'background:rgba(148,163,184,0.07);border:1px solid rgba(148,163,184,0.20);font-size:0.84rem;">'
            f'<div><span style="opacity:0.75;font-weight:600;text-transform:uppercase;font-size:0.74rem;'
            f'letter-spacing:0.04em;">Total Net Worth</span> &nbsp; '
            f'<strong style="font-size:0.98rem;font-variant-numeric:tabular-nums;">'
            f'{html.escape(format_inr(total_net_worth))}</strong></div>'
            f'<div style="opacity:0.80;font-variant-numeric:tabular-nums;">'
            f'Market: {html.escape(format_inr_compact(cur_total))} &nbsp;+&nbsp; {alt_status}'
            '</div>'
            '</div>'
        )
        st.markdown(nw_html, unsafe_allow_html=True)

    # 2. Asset Allocation & Top Positions / Owner Comparison
    eq_val = float(totals.get("equity_current_value", 0.0))
    mf_val = float(totals.get("mf_current_value", 0.0))

    if selection.owner is None and selection.account_id is None and holdings:
        col_left, col_mid, col_right = st.columns([1.0, 1.15, 1.0])
    else:
        col_left, col_right = st.columns([1.0, 1.25])
        col_mid = None

    with col_left:
        alloc_rows = [
            {"Asset Class": "Direct Equity & ETFs", "Value": eq_val},
            {"Asset Class": "Mutual Funds", "Value": mf_val},
        ]
        alloc_df = pd.DataFrame([r for r in alloc_rows if r["Value"] > 0])
        if not alloc_df.empty:
            fig_alloc = px.pie(
                alloc_df,
                names="Asset Class",
                values="Value",
                hole=0.60,
                color="Asset Class",
                color_discrete_map={
                    "Direct Equity & ETFs": COLOR_PALETTE[0],
                    "Mutual Funds": COLOR_PALETTE[1],
                },
            )
            fig_alloc.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="%{label}<br>₹%{value:,.0f} (%{percent})<extra></extra>",
            )
            style_plotly_fig(fig_alloc, title="Asset Allocation", height=340)
            fig_alloc.update_layout(showlegend=False, hovermode="closest")
            st.plotly_chart(fig_alloc, width="stretch")
        else:
            st.info("No positive valuations to plot allocation.")

    if col_mid is not None:
        with col_mid:
            by_owner: dict[str, dict[str, float]] = {}
            for h in holdings:
                ow = str(h.get("owner") or "Unknown")
                grp = by_owner.setdefault(ow, {"Invested": 0.0, "Current Value": 0.0})
                grp["Invested"] += float(h.get("invested_value", 0.0))
                grp["Current Value"] += float(h.get("current_value", 0.0))
            owner_df = pd.DataFrame(
                [
                    {
                        "Owner": ow,
                        "Invested": round(vals["Invested"], 2),
                        "Current Value": round(vals["Current Value"], 2),
                    }
                    for ow, vals in sorted(by_owner.items())
                ]
            )
            if not owner_df.empty:
                fig_owner = go.Figure()
                fig_owner.add_bar(
                    name="Invested",
                    x=owner_df["Owner"],
                    y=owner_df["Invested"],
                    marker_color="#94A3B8",
                    hovertemplate="%{x} Invested: ₹%{y:,.0f}<extra></extra>",
                )
                fig_owner.add_bar(
                    name="Current Value",
                    x=owner_df["Owner"],
                    y=owner_df["Current Value"],
                    marker_color=COLOR_PALETTE[0],
                    hovertemplate="%{x} Current: ₹%{y:,.0f}<extra></extra>",
                )
                fig_owner.update_layout(barmode="group")
                style_plotly_fig(fig_owner, title="Owner Comparison (₹)", height=340)
                st.plotly_chart(fig_owner, width="stretch")

    with col_right:
        if selection.owner is None and selection.account_id is None and holdings:
            by_owner_share: dict[str, float] = {}
            for h in holdings:
                ow = str(h.get("owner") or "Unknown")
                by_owner_share[ow] = by_owner_share.get(ow, 0.0) + float(h.get("current_value", 0.0))
            share_df = pd.DataFrame(
                [{"Owner": k, "Value": v} for k, v in by_owner_share.items() if v > 0]
            )
            if not share_df.empty:
                fig_share = px.pie(
                    share_df,
                    names="Owner",
                    values="Value",
                    hole=0.60,
                    color_discrete_sequence=COLOR_PALETTE,
                )
                fig_share.update_traces(
                    textposition="inside",
                    textinfo="percent+label",
                    hovertemplate="%{label}<br>₹%{value:,.0f} (%{percent})<extra></extra>",
                )
                style_plotly_fig(fig_share, title="Family Share by Owner", height=340)
                fig_share.update_layout(showlegend=False, hovermode="closest")
                st.plotly_chart(fig_share, width="stretch")
        else:
            top_items = holdings[:8]
            if top_items:
                top_df = pd.DataFrame(
                    [
                        {
                            "Name": (
                                str(x.get("symbol"))
                                if x.get("asset_class") == "EQUITY"
                                else str(x.get("scheme_name", ""))[:26]
                            ),
                            "Value": float(x.get("current_value", 0.0)),
                            "Weight": float(x.get("weight_pct", 0.0)),
                        }
                        for x in top_items
                    ]
                ).iloc[::-1]
                fig_top = px.bar(
                    top_df,
                    x="Value",
                    y="Name",
                    orientation="h",
                    text=top_df["Weight"].map(lambda w: f"{w:.1f}%"),
                    color_discrete_sequence=[COLOR_PALETTE[0]],
                )
                fig_top.update_traces(
                    textposition="outside",
                    hovertemplate="%{y}<br>₹%{x:,.0f}<extra></extra>",
                )
                style_plotly_fig(fig_top, title="Top 8 Positions by Value", height=340)
                fig_top.update_layout(hovermode="closest", yaxis_title="", xaxis_title="Current Value (₹)")
                st.plotly_chart(fig_top, width="stretch")

    st.divider()

    # 3. Symmetrical 2-Column Strategy & Geography Section
    if holdings:
        adv_df = _build_group_summary_df(holdings, "advisory_source", "Self")
        reg_df = _build_group_summary_df(holdings, "region", "India")

        col_adv, col_reg = st.columns(2)
        with col_adv:
            st.subheader("Advisory Breakdown")
            if not adv_df.empty:
                fig_adv = go.Figure()
                fig_adv.add_bar(
                    name="Invested",
                    x=adv_df["advisory_source"],
                    y=adv_df["invested_value"],
                    marker_color="#94A3B8",
                    hovertemplate="%{x} Invested: ₹%{y:,.0f}<extra></extra>",
                )
                fig_adv.add_bar(
                    name="Current",
                    x=adv_df["advisory_source"],
                    y=adv_df["current_value"],
                    marker_color=COLOR_PALETTE[0],
                    hovertemplate="%{x} Current: ₹%{y:,.0f}<extra></extra>",
                )
                fig_adv.update_layout(barmode="group")
                style_plotly_fig(fig_adv, title="Invested vs Current by Advisory (₹)", height=270)
                st.plotly_chart(fig_adv, width="stretch")

                st.dataframe(
                    adv_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "advisory_source": "Advisory",
                        "positions": st.column_config.NumberColumn("Count", format="%d"),
                        "invested_value": st.column_config.NumberColumn("Invested (₹)", format="₹%,.0f"),
                        "current_value": st.column_config.NumberColumn("Current (₹)", format="₹%,.0f"),
                        "unrealized_pnl": st.column_config.NumberColumn("P&L (₹)", format="₹%,.0f"),
                        "unrealized_pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
                        "weight_pct": st.column_config.NumberColumn("Wt %", format="%.2f%%"),
                    },
                )

        with col_reg:
            st.subheader("Domestic vs. Foreign")
            if not reg_df.empty:
                fig_reg = go.Figure()
                fig_reg.add_bar(
                    name="Invested",
                    x=reg_df["region"],
                    y=reg_df["invested_value"],
                    marker_color="#94A3B8",
                    hovertemplate="%{x} Invested: ₹%{y:,.0f}<extra></extra>",
                )
                fig_reg.add_bar(
                    name="Current",
                    x=reg_df["region"],
                    y=reg_df["current_value"],
                    marker_color=COLOR_PALETTE[1],
                    hovertemplate="%{x} Current: ₹%{y:,.0f}<extra></extra>",
                )
                fig_reg.update_layout(barmode="group")
                style_plotly_fig(fig_reg, title="Invested vs Current by Region (₹)", height=270)
                st.plotly_chart(fig_reg, width="stretch")

                st.dataframe(
                    reg_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "region": "Region",
                        "positions": st.column_config.NumberColumn("Count", format="%d"),
                        "invested_value": st.column_config.NumberColumn("Invested (₹)", format="₹%,.0f"),
                        "current_value": st.column_config.NumberColumn("Current (₹)", format="₹%,.0f"),
                        "unrealized_pnl": st.column_config.NumberColumn("P&L (₹)", format="₹%,.0f"),
                        "unrealized_pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
                        "weight_pct": st.column_config.NumberColumn("Wt %", format="%.2f%%"),
                    },
                )

    st.divider()

    # 4. Market-Cap Distribution (Instant SQLite cache + 1-click Gemini refresh)
    if holdings:
        c_cap_hdr, c_cap_seg, c_cap_btn = st.columns([2.2, 1.4, 1.1], vertical_alignment="bottom")
        with c_cap_hdr:
            st.subheader("Market-Cap Distribution")
        with c_cap_seg:
            adv_choices = sorted({str(h.get("advisory_source") or "Self").strip() for h in holdings})
            seg_options = ["All Combined", "Equities Only", "Mutual Funds Only"] + adv_choices
            cap_segment = st.selectbox(
                "Segment",
                options=seg_options,
                index=0,
                key="overview_cap_segment",
            )
        with c_cap_btn:
            refresh_cap = st.button(
                "Refresh Cap Split",
                icon=":material/auto_awesome:",
                width="stretch",
                key="overview_refresh_cap_btn",
                help="Sends your unique stock & MF names to Gemini once and caches Large/Mid/Small % in SQLite.",
            )

        if refresh_cap:
            api_key = st.session_state.get("gemini_api_key") or load_env_api_key()
            if not api_key:
                st.warning("Set your `GEMINI_API_KEY` in **Ask Gemini → Settings & Configuration** first.")
            else:
                model_choice = st.session_state.get("gemini_model_choice", "gemini-3.6-flash")
                with st.spinner("Fetching Large / Mid / Small Cap split from Gemini..."):
                    try:
                        cap_res = fetch_cap_allocations_via_gemini(
                            holdings=holdings,
                            api_key=api_key,
                            model=model_choice,
                            db_path=db_path,
                        )
                        st.toast(f"Updated cap split for {cap_res['saved']} assets via {cap_res['model']}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Cap split refresh failed: {exc}")

        cap_map = query_cap_allocations(db_path=db_path)
        cap_dist = compute_cap_distribution(holdings, cap_map, segment=cap_segment)
        if cap_dist["mapped_count"] == 0:
            st.info(
                "No cached Large/Mid/Small Cap split yet. Click **Refresh Cap Split** to fetch approximate weights "
                "from Gemini once (saved locally for instant loading)."
            )
        else:
            cap_tiles_html = (
                '<div class="tech-grid" style="margin-bottom:0.85rem;">'
                '<div class="tech-tile">'
                '<div class="tech-tile-label">Large Cap</div>'
                f'<div class="tech-tile-val">{cap_dist["large_pct"]:.1f}%</div>'
                f'<div class="tech-tile-sub">{html.escape(format_inr_compact(cap_dist["large_val"]))}</div>'
                '</div>'
                '<div class="tech-tile">'
                '<div class="tech-tile-label">Mid Cap</div>'
                f'<div class="tech-tile-val">{cap_dist["mid_pct"]:.1f}%</div>'
                f'<div class="tech-tile-sub">{html.escape(format_inr_compact(cap_dist["mid_val"]))}</div>'
                '</div>'
                '<div class="tech-tile">'
                '<div class="tech-tile-label">Small Cap</div>'
                f'<div class="tech-tile-val">{cap_dist["small_pct"]:.1f}%</div>'
                f'<div class="tech-tile-sub">{html.escape(format_inr_compact(cap_dist["small_val"]))}</div>'
                '</div>'
                '<div class="tech-tile">'
                '<div class="tech-tile-label">Other / Coverage</div>'
                f'<div class="tech-tile-val">{cap_dist["other_pct"]:.1f}%</div>'
                f'<div class="tech-tile-sub">Mapped: {cap_dist["mapped_count"]}/{cap_dist["total_count"]} ({cap_dist["coverage_pct"]:.0f}%) · {html.escape(cap_dist["last_updated"])}</div>'
                '</div>'
                '</div>'
            )
            st.markdown(cap_tiles_html, unsafe_allow_html=True)

            cap_chart_df = pd.DataFrame(
                [
                    {"Bucket": b, "Value": v, "Share": p}
                    for b, v, p in [
                        ("Large Cap", cap_dist["large_val"], cap_dist["large_pct"]),
                        ("Mid Cap", cap_dist["mid_val"], cap_dist["mid_pct"]),
                        ("Small Cap", cap_dist["small_val"], cap_dist["small_pct"]),
                        ("Other / Debt", cap_dist["other_val"], cap_dist["other_pct"]),
                    ]
                    if v > 0
                ]
            )
            if not cap_chart_df.empty:
                fig_cap = px.bar(
                    cap_chart_df,
                    x="Share",
                    y="Bucket",
                    orientation="h",
                    text=cap_chart_df.apply(lambda r: f"{r['Share']:.1f}% ({format_inr_compact(r['Value'])})", axis=1),
                    color="Bucket",
                    color_discrete_sequence=COLOR_PALETTE[:4],
                )
                fig_cap.update_traces(textposition="outside", hovertemplate="%{y}: %{x:.1f}%<extra></extra>")
                style_plotly_fig(fig_cap, title=f"Cap Split — {cap_segment}", height=240)
                fig_cap.update_layout(showlegend=False, hovermode="closest", yaxis_title="", xaxis_title="Weight %", xaxis_range=[0, 115])
                st.plotly_chart(fig_cap, width="stretch")

    st.divider()

    # 5. System, Sync & Tech Settings Section
    baselines = query_holding_baselines(
        owner=selection.owner, account_id=selection.account_id, db_path=db_path
    )
    baseline_dt = max((str(b["as_of_date"]) for b in baselines), default="None")
    db_stat = selection.db_status
    last_log = db_stat.get("last_log") or {}
    last_import_str = (
        f"{last_log.get('source_type', '—')} ({last_log.get('rows_inserted', 0)} rows)"
        if last_log
        else "No imports logged"
    )

    with st.expander("System, Sync & Architecture Settings", expanded=False):
        tech_html = (
            '<div class="tech-grid">'
            '<div class="tech-tile">'
            '<div class="tech-tile-label">Local SQLite Store</div>'
            f'<div class="tech-tile-val">{html.escape(db_path.name)}</div>'
            f'<div class="tech-tile-sub">{db_stat.get("eq_count", 0)} Eq · {db_stat.get("mf_count", 0)} MF · {db_stat.get("tx_count", 0)} Txns</div>'
            '</div>'
            '<div class="tech-tile">'
            '<div class="tech-tile-label">Depository Baseline</div>'
            f'<div class="tech-tile-val">{html.escape(baseline_dt)}</div>'
            f'<div class="tech-tile-sub">{len(baselines)} CDSL baseline positions</div>'
            '</div>'
            '<div class="tech-tile">'
            '<div class="tech-tile-label">Live Market Feeds</div>'
            '<div class="tech-tile-val">Yahoo Finance + AMFI</div>'
            '<div class="tech-tile-sub">In-memory TTL cache only (15 min)</div>'
            '</div>'
            '<div class="tech-tile">'
            '<div class="tech-tile-label">Last Import &amp; Backup</div>'
            f'<div class="tech-tile-val">{html.escape(last_import_str)}</div>'
            '<div class="tech-tile-sub">Run ./scripts/package_backup.sh</div>'
            '</div>'
            '</div>'
        )
        st.markdown(tech_html, unsafe_allow_html=True)
        st.caption(
            "Use **Refresh Prices** / **Restart App** in the left sidebar anytime, **Upload & Refresh** for monthly 5paisa/CAMS updates, "
            "and **Ask Gemini → Settings & Configuration** to switch Gemini models or update your API key."
        )
