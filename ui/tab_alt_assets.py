"""Alternative & Fixed Assets tab: Retirement/PPF, Real Estate, Net Worth mix, and live SQLite editor."""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from core.db import query_other_assets, save_other_assets
from ui.data_service import invalidate_all_caches
from ui.formatters import (
    COLOR_PALETTE,
    format_inr,
    format_inr_compact,
    style_plotly_fig,
)
from ui.sidebar import SidebarSelection


def render_alt_assets_tab(snapshot: dict[str, Any], selection: SidebarSelection) -> None:
    """Render the dedicated Alternative & Fixed Assets + Total Net Worth tab."""
    db_path: Path = selection.db_path
    totals = snapshot.get("totals", {})
    eq_val = float(totals.get("equity_current_value", 0.0))
    mf_val = float(totals.get("mf_current_value", 0.0))
    market_val = float(totals.get("current_value", 0.0))

    other_rows = query_other_assets(owner=selection.owner, db_path=db_path)
    alt_invested = sum(float(r.get("invested_value") or 0.0) for r in other_rows)
    alt_current = sum(float(r.get("current_value") or 0.0) for r in other_rows)

    ret_val = sum(
        float(r.get("current_value") or 0.0)
        for r in other_rows
        if "retirement" in str(r.get("category") or "").lower()
        or "ppf" in str(r.get("category") or "").lower()
    )
    re_val = sum(
        float(r.get("current_value") or 0.0)
        for r in other_rows
        if "real estate" in str(r.get("category") or "").lower()
        or "land" in str(r.get("category") or "").lower()
    )
    other_cat_val = max(0.0, alt_current - ret_val - re_val)
    total_net_worth = market_val + alt_current
    alt_share_pct = (alt_current / total_net_worth * 100.0) if total_net_worth > 0 else 0.0

    st.subheader("Alternative & Fixed Assets")
    st.caption(
        "Long-term static wealth (NPS, EPFO, PPF, Real Estate & Land) tracked separately from daily market analytics "
        "and rolled into Total Family Net Worth. Update valuations inline below and click Save."
    )

    summary_html = (
        '<div class="tech-grid" style="margin-bottom:1.1rem;">'
        '<div class="tech-tile">'
        '<div class="tech-tile-label">Retirement &amp; PPF</div>'
        f'<div class="tech-tile-val">{html.escape(format_inr(ret_val))}</div>'
        '<div class="tech-tile-sub">NPS · EPFO · PPF</div>'
        '</div>'
        '<div class="tech-tile">'
        '<div class="tech-tile-label">Real Estate &amp; Land</div>'
        f'<div class="tech-tile-val">{html.escape(format_inr(re_val))}</div>'
        '<div class="tech-tile-sub">Residential · Land Parcels</div>'
        '</div>'
        '<div class="tech-tile">'
        '<div class="tech-tile-label">Alternative Subtotal</div>'
        f'<div class="tech-tile-val">{html.escape(format_inr(alt_current))}</div>'
        f'<div class="tech-tile-sub">Invested: {html.escape(format_inr_compact(alt_invested))} · Other: {html.escape(format_inr_compact(other_cat_val))}</div>'
        '</div>'
        '<div class="tech-tile">'
        '<div class="tech-tile-label">Combined Net Worth</div>'
        f'<div class="tech-tile-val">{html.escape(format_inr(total_net_worth))}</div>'
        f'<div class="tech-tile-sub">Market: {html.escape(format_inr_compact(market_val))} · Alt: {alt_share_pct:.1f}%</div>'
        '</div>'
        '</div>'
    )
    st.markdown(summary_html, unsafe_allow_html=True)

    # Net Worth Mix Chart (only when alternative valuations > 0)
    if alt_current > 0 and total_net_worth > 0:
        c_chart, c_cat = st.columns([1.05, 1.0])
        with c_chart:
            nw_rows = [
                {"Bucket": "Direct Equity & ETFs", "Value": eq_val},
                {"Bucket": "Mutual Funds", "Value": mf_val},
                {"Bucket": "Retirement & PPF", "Value": ret_val},
                {"Bucket": "Real Estate & Land", "Value": re_val},
                {"Bucket": "Fixed / Other", "Value": other_cat_val},
            ]
            nw_df = pd.DataFrame([r for r in nw_rows if r["Value"] > 0])
            fig_nw = px.pie(
                nw_df,
                names="Bucket",
                values="Value",
                hole=0.58,
                color_discrete_sequence=COLOR_PALETTE,
            )
            fig_nw.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="%{label}<br>₹%{value:,.0f} (%{percent})<extra></extra>",
            )
            style_plotly_fig(fig_nw, title="Total Net Worth Composition", height=300)
            fig_nw.update_layout(showlegend=False, hovermode="closest")
            st.plotly_chart(fig_nw, width="stretch")

        with c_cat:
            cat_groups: dict[str, dict[str, float]] = {}
            for r in other_rows:
                cat = str(r.get("category") or "Other")
                grp = cat_groups.setdefault(cat, {"Items": 0, "Invested": 0.0, "Current": 0.0})
                grp["Items"] += 1
                grp["Invested"] += float(r.get("invested_value") or 0.0)
                grp["Current"] += float(r.get("current_value") or 0.0)
            cat_df = pd.DataFrame(
                [
                    {
                        "Category": k,
                        "Items": int(v["Items"]),
                        "Invested (₹)": round(v["Invested"], 0),
                        "Current (₹)": round(v["Current"], 0),
                        "Alt Share %": round((v["Current"] / alt_current * 100.0) if alt_current > 0 else 0.0, 1),
                    }
                    for k, v in sorted(cat_groups.items(), key=lambda x: x[1]["Current"], reverse=True)
                ]
            )
            st.markdown("##### Category Breakdown")
            st.dataframe(cat_df, width="stretch", hide_index=True)

        st.divider()

    st.markdown("##### Edit & Add Alternative Assets")
    alt_show_meta = st.toggle(
        "Show valuation date & notes",
        value=False,
        key=f"alt_show_meta_{selection.owner or 'all'}",
    )
    all_editor_cols = ["owner", "name", "category", "invested_value", "current_value", "as_of_date", "notes"]
    visible_cols = (
        all_editor_cols
        if alt_show_meta
        else (["owner"] if selection.owner is None else []) + ["name", "category", "invested_value", "current_value"]
    )
    alt_df = pd.DataFrame(other_rows)[visible_cols].copy() if other_rows else pd.DataFrame(columns=visible_cols)

    edited_alt_df = st.data_editor(
        alt_df,
        key=f"other_assets_editor_{selection.owner or 'all'}",
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "owner": st.column_config.SelectboxColumn(
                "Owner",
                options=["Abhishek", "Tanu", "Joint"],
                required=True,
                default=selection.owner or "Abhishek",
            ),
            "name": st.column_config.TextColumn("Asset Name", required=True, width="large"),
            "category": st.column_config.SelectboxColumn(
                "Category",
                options=["Retirement & PPF", "Real Estate & Land", "Fixed Income", "Other"],
                required=True,
                default="Retirement & PPF",
            ),
            "invested_value": st.column_config.NumberColumn(
                "Invested / Cost (₹)", min_value=0.0, format="₹%,.0f", default=0.0
            ),
            "current_value": st.column_config.NumberColumn(
                "Current Est. Value (₹)", min_value=0.0, format="₹%,.0f", default=0.0
            ),
            "as_of_date": st.column_config.TextColumn("Valuation Date", default=date.today().isoformat()),
            "notes": st.column_config.TextColumn("Notes / Folio / Unit"),
        },
    )

    btn_col, msg_col = st.columns([1.2, 3])
    with btn_col:
        if st.button(
            "Save Alternative Assets",
            type="primary",
            width="stretch",
            key=f"save_alt_btn_{selection.owner or 'all'}",
        ):
            existing_meta = {
                (str(r.get("owner") or "").strip().lower(), str(r.get("name") or "").strip().lower()): r
                for r in other_rows
            }
            payload: list[dict[str, Any]] = []
            for row in edited_alt_df.to_dict(orient="records"):
                nm = "" if pd.isna(row.get("name")) else str(row.get("name")).strip()
                if not nm:
                    continue
                ow = "" if pd.isna(row.get("owner")) else str(row.get("owner")).strip()
                eff_ow = ow or selection.owner or "Abhishek"
                prev = existing_meta.get((eff_ow.lower(), nm.lower()), {})
                cat = "" if pd.isna(row.get("category")) else str(row.get("category")).strip()
                inv_v = 0.0 if pd.isna(row.get("invested_value")) else float(row.get("invested_value"))
                cur_v = 0.0 if pd.isna(row.get("current_value")) else float(row.get("current_value"))
                dt_v = (
                    ("" if pd.isna(row.get("as_of_date")) else str(row.get("as_of_date")).strip())
                    if "as_of_date" in row
                    else str(prev.get("as_of_date") or "").strip()
                )
                note_v = (
                    ("" if pd.isna(row.get("notes")) else str(row.get("notes")).strip())
                    if "notes" in row
                    else str(prev.get("notes") or "").strip()
                )
                payload.append(
                    {
                        "owner": eff_ow,
                        "name": nm,
                        "category": cat or "Other",
                        "invested_value": inv_v,
                        "current_value": cur_v,
                        "as_of_date": dt_v or date.today().isoformat(),
                        "notes": note_v,
                    }
                )
            res = save_other_assets(payload, owner=selection.owner, replace_scope=True, db_path=db_path)
            invalidate_all_caches(clear_live_market=False)
            st.session_state["other_assets_saved_msg"] = f"Saved {res['saved']} alternative asset(s) to database."
            st.rerun()
    with msg_col:
        saved_msg = st.session_state.pop("other_assets_saved_msg", None)
        if saved_msg:
            st.success(saved_msg)
