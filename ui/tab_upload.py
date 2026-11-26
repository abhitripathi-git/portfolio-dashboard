"""Upload & Refresh tab: 5paisa, CAMS CAS, Quick Monthly Delta, Accounts, Aliases & Logs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from config import DEFAULT_OWNERS
from core.db import (
    get_import_logs,
    get_or_create_account,
    get_symbol_aliases,
    list_accounts,
    query_holdings,
    query_transactions,
    rebuild_holdings_from_transactions,
    save_symbol_alias,
)
from ingestion import (
    add_manual_trades,
    import_cams_cas,
    import_fivepaisa_file,
    parse_cams_cas,
    parse_fivepaisa_file,
    parse_manual_trades,
)
from ui.data_service import invalidate_all_caches
from ui.sidebar import SidebarSelection


def _owner_picker(key_prefix: str, db_path: Path, default_owner: str | None) -> str:
    """Reusable owner selector supporting existing owners + creating a new owner inline."""
    accounts = list_accounts(db_path=db_path)
    owners = sorted({a["owner"] for a in accounts if a.get("owner")} | set(DEFAULT_OWNERS))
    options = owners + ["+ New Owner"]
    default_idx = 0
    if default_owner and default_owner in owners:
        default_idx = owners.index(default_owner)

    choice = st.selectbox(
        "Owner",
        options=options,
        index=default_idx,
        key=f"{key_prefix}_owner_select",
    )
    if choice == "+ New Owner":
        new_owner = st.text_input(
            "New Owner Name",
            placeholder="e.g. Aarav",
            key=f"{key_prefix}_new_owner",
        ).strip()
        return new_owner
    return choice


def _render_fivepaisa_section(selection: SidebarSelection) -> None:
    st.markdown("#### 1. 5paisa / Broker File Upload (Tradebook or Holdings)")
    db_path = selection.db_path

    col_file, col_opts = st.columns([1.3, 1.0])
    with col_file:
        up_file = st.file_uploader(
            "Drag & drop 5paisa CSV, Excel, or PDF",
            type=["csv", "xlsx", "xls", "pdf"],
            key="fivepaisa_uploader",
        )
    with col_opts:
        owner_name = _owner_picker("fivepaisa", db_path, selection.owner)
        c_b, c_k = st.columns(2)
        with c_b:
            broker = st.text_input("Broker", value="5paisa", key="fivepaisa_broker").strip() or "5paisa"
        with c_k:
            kind = st.selectbox(
                "File Kind",
                options=["auto", "transactions", "holdings"],
                index=0,
                key="fivepaisa_kind",
            )
        cb1, cb2 = st.columns(2)
        with cb1:
            replace_holdings = st.checkbox(
                "Replace holdings",
                value=False,
                key="fivepaisa_replace_holdings",
                help="For holdings files: clears existing holdings for this account before upserting.",
            )
        with cb2:
            rebuild_holdings = st.checkbox(
                "Rebuild holdings",
                value=True,
                key="fivepaisa_rebuild_holdings",
                help="For transaction files: recompute net equity holdings from all account transactions.",
            )

    if up_file is not None:
        raw_bytes = up_file.getvalue()
        b_prev, b_imp, _ = st.columns([1.0, 1.2, 2.0])
        with b_prev:
            if st.button("Preview File", width="stretch", key="fivepaisa_preview_btn"):
                try:
                    parsed = parse_fivepaisa_file(raw_bytes, filename=up_file.name, kind=kind)
                    st.info(
                        f"Detected **{parsed['kind']}** · **{len(parsed['records'])}** rows"
                        + (
                            f" · Extra columns: `{', '.join(parsed['extra_columns'])}`"
                            if parsed.get("extra_columns")
                            else ""
                        )
                    )
                    if parsed["records"]:
                        st.dataframe(pd.DataFrame(parsed["records"][:15]), width="stretch", hide_index=True)
                    for w in parsed.get("warnings", []):
                        st.caption(f"Warning: {w}")
                except Exception as exc:
                    st.error(f"Preview failed: {exc}")

        with b_imp:
            if st.button("Import to Database", type="primary", width="stretch", key="fivepaisa_import_btn"):
                if not owner_name:
                    st.error("Please enter an owner name.")
                else:
                    try:
                        res = import_fivepaisa_file(
                            raw_bytes,
                            filename=up_file.name,
                            owner=owner_name,
                            kind=kind,
                            broker=broker,
                            replace_holdings=replace_holdings,
                            rebuild_holdings=rebuild_holdings,
                            db_path=db_path,
                        )
                        invalidate_all_caches(clear_live_market=False)
                        if res["kind"] == "transactions":
                            msg = (
                                f"Imported **{res.get('inserted', 0)}** new transactions "
                                f"({res.get('skipped', 0)} duplicates skipped) for **{res['account']['owner']}**."
                            )
                            if "rebuilt" in res:
                                msg += f" Rebuilt **{res['rebuilt'].get('upserted', 0)}** equity holdings."
                        else:
                            msg = (
                                f"Upserted **{res.get('upserted', 0)}** holdings "
                                f"for **{res['account']['owner']}**."
                            )
                        st.success(msg)
                        if res.get("extra_columns"):
                            st.caption(f"Preserved extra columns in `extra_data`: {', '.join(res['extra_columns'])}")
                        for w in res.get("warnings", []):
                            st.caption(f"Warning: {w}")
                    except Exception as exc:
                        st.error(f"Import failed: {exc}")


def _render_cams_section(selection: SidebarSelection) -> None:
    st.markdown("#### 2. CAMS / KFintech CAS Upload (Mutual Funds)")
    db_path = selection.db_path

    col_file, col_opts = st.columns([1.3, 1.0])
    with col_file:
        cas_file = st.file_uploader(
            "Drag & drop CAMS/KFintech CAS PDF, Excel, or CSV",
            type=["pdf", "xlsx", "xls", "csv"],
            key="cams_uploader",
        )
    with col_opts:
        owner_name = _owner_picker("cams", db_path, selection.owner)
        password = st.text_input(
            "PDF Password (optional)",
            type="password",
            placeholder="Usually PAN or DOB if encrypted",
            key="cams_password",
        )
        replace_mf = st.checkbox(
            "Replace existing MF holdings for this CAS account",
            value=True,
            key="cams_replace_mf",
        )

    if cas_file is not None:
        raw_bytes = cas_file.getvalue()
        b_prev, b_imp, _ = st.columns([1.0, 1.2, 2.0])
        with b_prev:
            if st.button("Preview CAS", width="stretch", key="cams_preview_btn"):
                try:
                    parsed = parse_cams_cas(raw_bytes, filename=cas_file.name, password=password)
                    st.info(
                        f"Parsed **{len(parsed.get('mf_holdings', []))}** MF schemes & "
                        f"**{len(parsed.get('mf_transactions', []))}** MF transactions."
                    )
                    if parsed.get("mf_holdings"):
                        st.dataframe(pd.DataFrame(parsed["mf_holdings"][:15]), width="stretch", hide_index=True)
                    for w in parsed.get("warnings", []):
                        st.caption(f"Warning: {w}")
                except Exception as exc:
                    st.error(f"CAS preview failed: {exc}")

        with b_imp:
            if st.button("Import CAS to Database", type="primary", width="stretch", key="cams_import_btn"):
                if not owner_name:
                    st.error("Please enter an owner name.")
                else:
                    try:
                        res = import_cams_cas(
                            raw_bytes,
                            filename=cas_file.name,
                            owner=owner_name,
                            password=password,
                            replace_mf=replace_mf,
                            db_path=db_path,
                        )
                        invalidate_all_caches(clear_live_market=False)
                        st.success(
                            f"Upserted **{res.get('upserted', 0)}** mutual fund holdings "
                            f"for **{res['account']['owner']}**."
                        )
                        for w in res.get("warnings", []):
                            st.caption(f"Warning: {w}")
                    except Exception as exc:
                        st.error(f"CAS import failed: {exc}")


def _render_manual_delta_section(selection: SidebarSelection) -> None:
    st.markdown("#### 3. Quick Monthly Delta (Manual Trade Entry)")
    db_path = selection.db_path

    col_txt, col_opts = st.columns([1.5, 1.0])
    with col_txt:
        delta_text = st.text_area(
            "Paste shorthand trades or CSV/TSV rows (one per line)",
            height=135,
            placeholder=(
                "2026-11-20 BUY RELIANCE 10 @ 2950\n"
                "2026-11-21 SELL TCS 5 @ 4100 fees=25\n"
                "2026-11-22 BONUS HDFCBANK 10"
            ),
            key="manual_delta_text",
        )
    with col_opts:
        owner_name = _owner_picker("manual", db_path, selection.owner)
        broker = st.text_input("Account Broker", value="5paisa", key="manual_broker").strip() or "5paisa"
        rebuild_flag = st.checkbox(
            "Rebuild equity holdings after insert",
            value=True,
            key="manual_rebuild_holdings",
        )

    b1, b2, _ = st.columns([1.0, 1.2, 2.0])
    with b1:
        if st.button("Preview Trades", width="stretch", key="manual_preview_btn"):
            if not delta_text.strip():
                st.warning("Enter at least one trade line to preview.")
            else:
                try:
                    trades = parse_manual_trades(delta_text)
                    st.info(f"Parsed **{len(trades)}** trade(s).")
                    if trades:
                        st.dataframe(pd.DataFrame(trades), width="stretch", hide_index=True)
                except Exception as exc:
                    st.error(f"Parse error: {exc}")

    with b2:
        if st.button("Commit Trades", type="primary", width="stretch", key="manual_commit_btn"):
            if not delta_text.strip():
                st.warning("Enter at least one trade line.")
            elif not owner_name:
                st.error("Please enter an owner name.")
            else:
                try:
                    res = add_manual_trades(
                        text=delta_text,
                        owner=owner_name,
                        broker=broker,
                        rebuild_holdings=rebuild_flag,
                        db_path=db_path,
                    )
                    invalidate_all_caches(clear_live_market=False)
                    msg = (
                        f"Inserted **{res.get('inserted', 0)}** trade(s) "
                        f"({res.get('skipped', 0)} skipped) for **{res['account']['owner']}**."
                    )
                    if "rebuilt" in res:
                        msg += f" Holdings updated ({res['rebuilt'].get('upserted', 0)} positions)."
                    st.success(msg)
                except Exception as exc:
                    st.error(f"Failed to add manual trades: {exc}")


def _render_accounts_aliases_logs(selection: SidebarSelection) -> None:
    st.markdown("#### 4. Accounts, Symbol Aliases & Import Logs")
    db_path = selection.db_path

    tab_acct, tab_alias, tab_logs = st.tabs(
        ["Accounts Manager", "Symbol Aliases (Yahoo Mapping)", "Import Logs"]
    )

    with tab_acct:
        accounts = list_accounts(db_path=db_path)
        if accounts:
            acct_df = pd.DataFrame(accounts)[
                ["id", "owner", "broker", "account_name", "account_code", "created_at"]
            ]
            st.dataframe(acct_df, width="stretch", hide_index=True)
        else:
            st.caption("No accounts found.")

        with st.form("create_account_form", clear_on_submit=True):
            st.markdown("**Create New Owner / Account**")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                new_owner = st.text_input("Owner Name", placeholder="e.g. Abhishek")
            with c2:
                new_broker = st.text_input("Broker", value="5paisa")
            with c3:
                new_acct_name = st.text_input("Account Label (optional)", placeholder="e.g. Zerodha")
            with c4:
                new_code = st.text_input("Client Code (optional)", placeholder="e.g. 5P12345")
            submitted = st.form_submit_button("Create / Fetch Account", type="primary")
            if submitted:
                if not new_owner.strip():
                    st.error("Owner name is required.")
                else:
                    acct = get_or_create_account(
                        owner=new_owner,
                        broker=new_broker or "5paisa",
                        account_name=new_acct_name.strip() or None,
                        account_code=new_code.strip(),
                        db_path=db_path,
                    )
                    invalidate_all_caches(clear_live_market=False)
                    st.success(f"Account ready: **{acct['account_name']}** (id={acct['id']}).")
                    st.rerun()

        # Quick helper to rebuild holdings from transactions for an account
        if accounts:
            with st.expander("Rebuild Equity Holdings from Transactions", expanded=False):
                target_acct = st.selectbox(
                    "Select Account",
                    options=accounts,
                    format_func=lambda a: f"{a['owner']} — {a['account_name']} ({a['broker']})",
                    key="rebuild_acct_select",
                )
                if st.button("Rebuild Holdings Now", key="rebuild_now_btn"):
                    res = rebuild_holdings_from_transactions(target_acct["id"], db_path=db_path)
                    invalidate_all_caches(clear_live_market=False)
                    st.success(f"Rebuilt {res.get('upserted', 0)} holding(s) for {target_acct['account_name']}.")
                    st.rerun()

    with tab_alias:
        aliases = get_symbol_aliases(db_path=db_path)
        held_syms = {h["symbol"] for h in query_holdings(db_path=db_path) if h.get("symbol")}
        tx_syms = {
            t["symbol"]
            for t in query_transactions(db_path=db_path)
            if t.get("symbol") and str(t.get("asset_type", "EQUITY")).upper() != "MF"
        }
        unmapped = sorted((held_syms | tx_syms) - set(aliases.keys()))
        if unmapped:
            st.warning(
                f"Symbols without saved alias in `symbol_aliases`: **{', '.join(unmapped)}**"
            )

        if aliases:
            alias_rows = sorted(aliases.values(), key=lambda x: x["raw_symbol"])
            st.dataframe(
                pd.DataFrame(alias_rows)[
                    ["raw_symbol", "yahoo_symbol", "isin", "company_name", "sector", "updated_at"]
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No saved symbol aliases yet.")

        with st.form("save_alias_form", clear_on_submit=False):
            st.markdown("**Add or Fix Symbol Alias (`raw_symbol -> yahoo_symbol`)**")
            a1, a2, a3, a4, a5 = st.columns([1.0, 1.1, 1.0, 1.2, 0.9])
            with a1:
                raw_sym = st.text_input(
                    "Raw Symbol",
                    value=unmapped[0] if unmapped else "",
                    placeholder="e.g. TATAMOTORS",
                )
            with a2:
                yahoo_sym = st.text_input(
                    "Yahoo Symbol",
                    placeholder="e.g. TATAMOTORS.NS or BOM:500570",
                )
            with a3:
                isin_val = st.text_input("ISIN (optional)", placeholder="INE155A01022")
            with a4:
                comp_val = st.text_input("Company Name (optional)")
            with a5:
                sector_val = st.text_input("Sector (optional)")

            save_btn = st.form_submit_button("Save Alias", type="primary")
            if save_btn:
                if not raw_sym.strip() or not yahoo_sym.strip():
                    st.error("Both Raw Symbol and Yahoo Symbol are required.")
                else:
                    save_symbol_alias(
                        raw_symbol=raw_sym,
                        yahoo_symbol=yahoo_sym,
                        isin=isin_val,
                        company_name=comp_val,
                        sector=sector_val,
                        db_path=db_path,
                    )
                    invalidate_all_caches(clear_live_market=True)
                    st.success(f"Saved alias `{raw_sym.strip().upper()} -> {yahoo_sym.strip()}`.")
                    st.rerun()

    with tab_logs:
        logs = get_import_logs(limit=50, db_path=db_path)
        if logs:
            st.dataframe(
                pd.DataFrame(logs)[
                    ["id", "imported_at", "owner", "account_name", "source_type", "filename", "rows_inserted", "rows_skipped", "notes"]
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No import logs recorded yet.")


def render_upload_tab(selection: SidebarSelection) -> None:
    """Render the Upload & Refresh tab."""
    st.subheader(f"Upload, Manual Trades & Data Manager (`{selection.db_path.name}`)")
    _render_fivepaisa_section(selection)
    st.divider()
    _render_cams_section(selection)
    st.divider()
    _render_manual_delta_section(selection)
    st.divider()
    _render_accounts_aliases_logs(selection)
