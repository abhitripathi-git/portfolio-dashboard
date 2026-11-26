"""Holdings & Movers tab: Equity & MF tables with extra_data, gainers/losers chart, and transactions."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.db import query_transactions
from ui.formatters import (
    GAIN_COLOR,
    LOSS_COLOR,
    expand_extra_data_columns,
    style_plotly_fig,
)
from ui.sidebar import SidebarSelection


def _render_movers_chart(holdings: list[dict[str, Any]]) -> None:
    st.subheader("Top Gainers & Top Losers")
    if not holdings:
        st.caption("No holdings available to chart movers.")
        return

    c_metric, c_scope = st.columns([1.2, 1.0])
    with c_metric:
        metric_choice = st.radio(
            "Rank By",
            options=["Unrealized P&L %", "Unrealized P&L ₹", "Day Change %"],
            horizontal=True,
            key="movers_metric_radio",
        )
    with c_scope:
        scope_choice = st.radio(
            "Asset Scope",
            options=["All Holdings", "Equities Only", "Mutual Funds Only"],
            horizontal=True,
            key="movers_scope_radio",
        )

    filtered = holdings
    if scope_choice == "Equities Only":
        filtered = [h for h in holdings if h.get("asset_class") == "EQUITY"]
    elif scope_choice == "Mutual Funds Only":
        filtered = [h for h in holdings if h.get("asset_class") == "MF"]

    if not filtered:
        st.caption("No positions match the selected asset scope.")
        return

    field_map = {
        "Unrealized P&L %": "unrealized_pnl_pct",
        "Unrealized P&L ₹": "unrealized_pnl",
        "Day Change %": "day_change_pct",
    }
    field = field_map[metric_choice]

    # Aggregate by symbol/scheme across accounts so Combined view doesn't duplicate tickers
    agg: dict[str, dict[str, Any]] = {}
    for h in filtered:
        is_eq = h.get("asset_class") == "EQUITY"
        label = str(h.get("symbol")) if is_eq else str(h.get("scheme_name", ""))[:32]
        entry = agg.setdefault(
            label,
            {
                "label": label,
                "invested": 0.0,
                "current": 0.0,
                "pnl": 0.0,
                "day_change": 0.0,
            },
        )
        entry["invested"] += float(h.get("invested_value", 0.0))
        entry["current"] += float(h.get("current_value", 0.0))
        entry["pnl"] += float(h.get("unrealized_pnl", 0.0))
        entry["day_change"] += float(h.get("day_change", 0.0))

    items: list[dict[str, Any]] = []
    for e in agg.values():
        inv = e["invested"]
        prev = e["current"] - e["day_change"]
        if field == "unrealized_pnl_pct":
            val = (e["pnl"] / inv * 100.0) if inv > 0 else 0.0
        elif field == "unrealized_pnl":
            val = e["pnl"]
        else:
            val = (e["day_change"] / prev * 100.0) if prev > 0 else 0.0
        items.append({"label": e["label"], "value": round(val, 2), "pnl": e["pnl"]})

    gainers = sorted([x for x in items if x["value"] > 0], key=lambda x: x["value"], reverse=True)[:8]
    losers = sorted([x for x in items if x["value"] < 0], key=lambda x: x["value"])[:8]

    if not gainers and not losers:
        st.caption("All positions currently have 0% change for the selected metric.")
        return

    combined = list(reversed(losers)) + list(reversed(gainers))
    labels = [x["label"] for x in combined]
    vals = [x["value"] for x in combined]
    colors = [GAIN_COLOR if v >= 0 else LOSS_COLOR for v in vals]
    is_pct = metric_choice != "Unrealized P&L ₹"
    text_labels = [f"{v:+.2f}%" if is_pct else f"₹{v:+,.0f}" for v in vals]

    fig = go.Figure(
        go.Bar(
            x=vals,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=text_labels,
            textposition="outside",
            hovertemplate="%{y}: %{text}<extra></extra>",
        )
    )
    fig.add_vline(x=0, line_color="rgba(148,163,184,0.6)", line_width=1)
    style_plotly_fig(
        fig,
        title=f"Movers by {metric_choice}",
        height=max(320, 28 * len(combined) + 90),
    )
    fig.update_layout(hovermode="closest", xaxis_title=metric_choice, yaxis_title="")
    st.plotly_chart(fig, width="stretch")


def render_holdings_tab(snapshot: dict[str, Any], selection: SidebarSelection) -> None:
    """Render the Holdings & Movers tab."""
    eq_holdings: list[dict[str, Any]] = snapshot.get("equity_holdings", [])
    mf_holdings: list[dict[str, Any]] = snapshot.get("mf_holdings", [])
    all_holdings: list[dict[str, Any]] = snapshot.get("holdings", [])

    # 1. Equity Holdings Table
    st.subheader(f"Equity Holdings ({len(eq_holdings)})")
    if eq_holdings:
        fc_eq1, fc_eq2, fc_eq3 = st.columns([1.8, 0.85, 0.95])
        with fc_eq1:
            search_eq = st.text_input(
                "Filter equities by symbol, company, owner, advisory, or custom field",
                placeholder="e.g. RELIANCE, MON100, Foreign, Self...",
                key="eq_holdings_search",
            ).strip().lower()
        with fc_eq2:
            region_options = sorted({str(h.get("region") or "India").strip() for h in eq_holdings})
            region_filter = st.multiselect(
                "Region",
                options=region_options,
                default=region_options,
                key="eq_region_filter",
            )
        with fc_eq3:
            adv_options = sorted({str(h.get("advisory_source") or "Self").strip() for h in eq_holdings})
            adv_filter = st.multiselect(
                "Advisory Source",
                options=adv_options,
                default=adv_options,
                key="eq_advisory_filter",
            )

        eq_df, eq_extra_cols = expand_extra_data_columns(
            eq_holdings,
            reserved_keys={
                "owner", "account_name", "symbol", "company_name", "asset_type", "exchange",
                "region", "advisory_source", "advisory_notes",
                "quantity", "avg_price", "current_price", "invested_value",
                "current_value", "unrealized_pnl", "unrealized_pnl_pct",
                "day_change", "day_change_pct", "weight_pct",
            },
        )
        if region_filter and "region" in eq_df.columns:
            eq_df = eq_df[eq_df["region"].isin(region_filter)]
        if adv_filter and "advisory_source" in eq_df.columns:
            eq_df = eq_df[eq_df["advisory_source"].isin(adv_filter)]
        if search_eq:
            mask = eq_df.astype(str).apply(lambda col: col.str.lower().str.contains(search_eq, na=False)).any(axis=1)
            eq_df = eq_df[mask]

        eq_show_all = st.toggle(
            "Show detailed columns (Company, Account, Advisory, Type, Extra)",
            value=False,
            key="eq_show_details",
        )
        compact_eq_cols = (["owner"] if selection.owner is None else []) + [
            "symbol", "quantity", "avg_price", "current_price",
            "invested_value", "current_value", "unrealized_pnl",
            "unrealized_pnl_pct", "day_change_pct", "weight_pct",
        ]
        detailed_eq_cols = [
            "owner", "account_name", "symbol", "company_name", "asset_type", "region",
            "advisory_source", "advisory_notes", "exchange", "quantity",
            "avg_price", "current_price", "invested_value",
            "current_value", "unrealized_pnl", "unrealized_pnl_pct",
            "day_change_pct", "weight_pct", *eq_extra_cols,
        ]
        target_eq_cols = detailed_eq_cols if eq_show_all else compact_eq_cols
        show_eq_cols = [c for c in target_eq_cols if c in eq_df.columns]
        st.dataframe(
            eq_df[show_eq_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "owner": "Owner",
                "account_name": "Account",
                "symbol": "Symbol",
                "company_name": "Company",
                "asset_type": "Type",
                "region": "Region",
                "advisory_source": "Advisory",
                "advisory_notes": "Advisory Notes",
                "exchange": "Exch",
                "quantity": st.column_config.NumberColumn("Qty", format="%.2f"),
                "avg_price": st.column_config.NumberColumn("Avg Price (₹)", format="₹%.2f"),
                "current_price": st.column_config.NumberColumn("LTP (₹)", format="₹%.2f"),
                "invested_value": st.column_config.NumberColumn("Invested (₹)", format="₹%,.0f"),
                "current_value": st.column_config.NumberColumn("Current (₹)", format="₹%,.0f"),
                "unrealized_pnl": st.column_config.NumberColumn("P&L (₹)", format="₹%,.0f"),
                "unrealized_pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
                "day_change_pct": st.column_config.NumberColumn("Day %", format="%.2f%%"),
                "weight_pct": st.column_config.NumberColumn("Weight %", format="%.2f%%"),
            },
        )
        st.download_button(
            "Download Equity Holdings CSV",
            data=eq_df[show_eq_cols].to_csv(index=False).encode("utf-8"),
            file_name=f"equity_holdings_{selection.owner_label.lower()}.csv",
            mime="text/csv",
            key="dl_eq_holdings_csv",
        )
    else:
        st.info("No equity holdings found for this selection.")

    st.divider()

    # 2. Mutual Fund Holdings Table
    st.subheader(f"Mutual Fund Holdings ({len(mf_holdings)})")
    if mf_holdings:
        mf_df, mf_extra_cols = expand_extra_data_columns(
            mf_holdings,
            reserved_keys={
                "owner", "account_name", "scheme_name", "folio_number", "amfi_code",
                "region", "advisory_source", "advisory_notes",
                "quantity", "avg_price", "current_price", "invested_value",
                "current_value", "unrealized_pnl", "unrealized_pnl_pct",
                "weight_pct", "nav_date",
            },
            skip_extra_keys={"constituents", "constituents_source", "constituents_confidence"},
        )
        mf_show_all = st.toggle(
            "Show detailed columns (Folio, AMFI Code, Account, Advisory, Extra)",
            value=False,
            key="mf_show_details",
        )
        compact_mf_cols = (["owner"] if selection.owner is None else []) + [
            "scheme_name", "quantity", "avg_price", "current_price",
            "invested_value", "current_value", "unrealized_pnl",
            "unrealized_pnl_pct", "weight_pct",
        ]
        detailed_mf_cols = [
            "owner", "account_name", "scheme_name", "folio_number", "amfi_code",
            "region", "advisory_source", "advisory_notes",
            "quantity", "avg_price", "current_price", "invested_value",
            "current_value", "unrealized_pnl", "unrealized_pnl_pct",
            "weight_pct", "nav_date", *mf_extra_cols,
        ]
        target_mf_cols = detailed_mf_cols if mf_show_all else compact_mf_cols
        show_mf_cols = [c for c in target_mf_cols if c in mf_df.columns]
        st.dataframe(
            mf_df[show_mf_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "owner": "Owner",
                "account_name": "Account",
                "scheme_name": "Scheme",
                "folio_number": "Folio",
                "amfi_code": "AMFI Code",
                "region": "Region",
                "advisory_source": "Advisory",
                "advisory_notes": "Advisory Notes",
                "quantity": st.column_config.NumberColumn("Units", format="%.3f"),
                "avg_price": st.column_config.NumberColumn("Avg NAV (₹)", format="₹%.2f"),
                "current_price": st.column_config.NumberColumn("Live NAV (₹)", format="₹%.2f"),
                "invested_value": st.column_config.NumberColumn("Invested (₹)", format="₹%,.0f"),
                "current_value": st.column_config.NumberColumn("Current (₹)", format="₹%,.0f"),
                "unrealized_pnl": st.column_config.NumberColumn("P&L (₹)", format="₹%,.0f"),
                "unrealized_pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
                "weight_pct": st.column_config.NumberColumn("Weight %", format="%.2f%%"),
                "nav_date": "NAV Date",
            },
        )
        st.download_button(
            "Download MF Holdings CSV",
            data=mf_df[show_mf_cols].to_csv(index=False).encode("utf-8"),
            file_name=f"mf_holdings_{selection.owner_label.lower()}.csv",
            mime="text/csv",
            key="dl_mf_holdings_csv",
        )
    else:
        st.info("No mutual fund holdings found for this selection.")

    st.divider()

    # 3. Movers Bar Chart
    _render_movers_chart(all_holdings)

    st.divider()

    # 4. Searchable / Filterable Transactions Table
    st.subheader("Transactions Ledger")
    txs = query_transactions(
        owner=selection.owner,
        account_id=selection.account_id,
        db_path=selection.db_path,
    )
    if not txs:
        st.info("No transactions recorded yet for this selection.")
        return

    fc1, fc2, fc3 = st.columns([1.4, 1.0, 1.0])
    with fc1:
        tx_search = st.text_input(
            "Search Symbol / ISIN / Custom Fields",
            placeholder="Type symbol or note...",
            key="tx_ledger_search",
        ).strip().lower()
    with fc2:
        all_sides = sorted({str(t.get("side", "")).upper() for t in txs if t.get("side")})
        if "tx_side_filter" not in st.session_state:
            st.session_state["tx_side_filter"] = all_sides
        else:
            st.session_state["tx_side_filter"] = [
                s for s in st.session_state["tx_side_filter"] if s in all_sides
            ] or all_sides
        side_filter = st.multiselect(
            "Side",
            options=all_sides,
            key="tx_side_filter",
        )
    with fc3:
        min_dt = min(str(t["trade_date"]) for t in txs)
        max_dt = max(str(t["trade_date"]) for t in txs)
        sort_desc = st.checkbox("Newest first", value=True, key="tx_sort_desc")
        st.caption(f"Date span: {min_dt} → {max_dt}")

    tx_df, tx_extra_cols = expand_extra_data_columns(
        txs,
        reserved_keys={
            "trade_date", "owner", "account_name", "symbol", "isin",
            "exchange", "asset_type", "side", "quantity", "price",
            "amount", "fees", "source", "advisory_source", "advisory_notes",
        },
    )
    if side_filter:
        tx_df = tx_df[tx_df["side"].isin(side_filter)]
    if tx_search:
        mask = tx_df.astype(str).apply(lambda col: col.str.lower().str.contains(tx_search, na=False)).any(axis=1)
        tx_df = tx_df[mask]
    if sort_desc and "trade_date" in tx_df.columns:
        tx_df = tx_df.sort_values(by=["trade_date", "id"], ascending=[False, False])

    tx_show_all = st.toggle(
        "Show detailed columns (Account, ISIN, Fees, Source, Notes)",
        value=False,
        key="tx_show_details",
    )
    compact_tx_cols = ["trade_date"] + (["owner"] if selection.owner is None else []) + [
        "symbol", "side", "quantity", "price", "amount",
    ]
    detailed_tx_cols = [
        "trade_date", "owner", "account_name", "symbol", "isin", "exchange",
        "side", "quantity", "price", "amount", "fees", "source",
        "advisory_source", "advisory_notes", *tx_extra_cols,
    ]
    target_tx_cols = detailed_tx_cols if tx_show_all else compact_tx_cols
    show_tx_cols = [c for c in target_tx_cols if c in tx_df.columns]
    st.caption(f"Showing {len(tx_df)} of {len(txs)} transactions")
    st.dataframe(
        tx_df[show_tx_cols],
        width="stretch",
        hide_index=True,
        column_config={
            "trade_date": "Date",
            "owner": "Owner",
            "account_name": "Account",
            "symbol": "Symbol",
            "isin": "ISIN",
            "exchange": "Exch",
            "side": "Side",
            "quantity": st.column_config.NumberColumn("Qty", format="%.4f"),
            "price": st.column_config.NumberColumn("Price (₹)", format="₹%.2f"),
            "amount": st.column_config.NumberColumn("Amount (₹)", format="₹%,.2f"),
            "fees": st.column_config.NumberColumn("Fees (₹)", format="₹%.2f"),
            "source": "Source",
            "advisory_source": "Advisory",
            "advisory_notes": "Advisory Notes",
        },
    )
    st.download_button(
        "Download Filtered Transactions CSV",
        data=tx_df[show_tx_cols].to_csv(index=False).encode("utf-8"),
        file_name=f"transactions_{selection.owner_label.lower()}.csv",
        mime="text/csv",
        key="dl_tx_csv",
    )
