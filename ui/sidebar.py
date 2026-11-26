"""Sidebar controls: owner/account filter, live price refresh, server restart, and DB status."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from config import DB_PATH, DEFAULT_OWNERS
from core.db import init_db, list_accounts
from core.server_control import schedule_server_restart, sync_backup_to_cloud, sync_code_to_github
from ui.data_service import invalidate_all_caches, load_db_status


@dataclass
class SidebarSelection:
    owner: str | None  # None means All (Combined)
    owner_label: str
    account_id: int | None
    account_label: str
    db_path: Path
    is_demo_mode: bool
    db_status: dict[str, Any]


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def render_sidebar() -> SidebarSelection:
    """Render clean sidebar controls and return active filter & DB selection."""
    with st.sidebar:
        st.title(":material/insights: Portfolio Lens")
        st.caption("Family Equity & Mutual Fund Analytics")

        active_db: Path = DB_PATH
        init_db(db_path=active_db, seed_default_owners=True)

        accounts = list_accounts(db_path=active_db)
        distinct_owners = sorted(
            {a["owner"] for a in accounts if a.get("owner")} | set(DEFAULT_OWNERS)
        )

        st.divider()
        st.subheader(":material/person: Portfolio Scope")

        owner_options = ["All (Combined)"] + distinct_owners
        selected_owner_opt = st.selectbox(
            "Owner",
            options=owner_options,
            index=0,
            key="sidebar_owner_select",
        )
        owner_val: str | None = None if selected_owner_opt == "All (Combined)" else selected_owner_opt
        owner_label = "Combined" if owner_val is None else owner_val

        filtered_accounts = (
            accounts
            if owner_val is None
            else [a for a in accounts if a["owner"].lower() == owner_val.lower()]
        )
        acct_options: list[tuple[int | None, str]] = [(None, "All Accounts")]
        for acct in filtered_accounts:
            acct_options.append(
                (int(acct["id"]), f"{acct['account_name']} ({acct['broker']})")
            )

        selected_acct_pair = st.selectbox(
            "Account (optional)",
            options=acct_options,
            format_func=lambda item: item[1],
            index=0,
            key="sidebar_account_select",
        )
        account_id, account_label = selected_acct_pair

        st.divider()
        b_refresh, b_restart = st.columns(2)
        with b_refresh:
            if st.button("Refresh", icon=":material/sync:", width="stretch", type="primary", key="sidebar_refresh_btn", help="Clear live price & NAV cache and fetch fresh quotes"):
                invalidate_all_caches(clear_live_market=True)
                st.toast("Cleared live price & NAV cache — fetching fresh quotes.")
                st.rerun()
        with b_restart:
            if st.button(
                "Restart",
                icon=":material/restart_alt:",
                width="stretch",
                key="sidebar_restart_btn",
                help="Kills the running Streamlit server and starts a fresh instance automatically.",
            ):
                port = st.get_option("server.port") or 8501
                schedule_server_restart(port=int(port), delay_seconds=0.6)
                st.warning("Restarting dashboard server — reloading automatically...")
                components.html(
                    "<script>(function(){const u=window.parent.location.href;async function p(){try{const r=await fetch(u,{cache:'no-store'});if(r.ok){window.parent.location.reload();return;}}catch(e){}setTimeout(p,800);}setTimeout(p,1400);})();</script>",
                    height=0,
                )
                st.stop()

        b_drive, b_git = st.columns(2)
        with b_drive:
            if st.button(
                "Drive DB",
                icon=":material/cloud_upload:",
                width="stretch",
                key="sidebar_backup_sync_btn",
                help="Atomically snapshots portfolio.db + portable zip to Google Drive.",
            ):
                with st.spinner("Syncing DB..."):
                    b_res = sync_backup_to_cloud(active_db)
                st.toast(f"Backup synced ({b_res['mode']})")
                st.session_state["sidebar_backup_msg"] = b_res["message"]
        with b_git:
            if st.button(
                "GitHub",
                icon=":material/code:",
                width="stretch",
                key="sidebar_github_sync_btn",
                help="Commits and pushes code changes to your private GitHub repo (excludes DB & .env).",
            ):
                with st.spinner("Pushing code..."):
                    g_res = sync_code_to_github()
                if g_res.get("ok"):
                    st.toast("Code synced to GitHub")
                else:
                    st.toast("GitHub sync needs attention", icon="⚠️")
                st.session_state["sidebar_backup_msg"] = str(g_res.get("message", ""))

        if st.session_state.get("sidebar_backup_msg"):
            st.caption(st.session_state["sidebar_backup_msg"])

        db_status = load_db_status(active_db)

        st.divider()
        st.subheader(":material/storage: Database Status")
        status_html = (
            '<div class="db-status-card">'
            f'<div><strong><code>{active_db.name}</code></strong> &nbsp;'
            '<span class="status-badge badge-high">LIVE DB</span></div>'
            f'<div>Size: <strong>{_format_size(db_status["size_bytes"])}</strong> · '
            f'Accounts: <strong>{db_status["accounts_count"]}</strong></div>'
            f'<div>Equities: <strong>{db_status["eq_count"]}</strong> · '
            f'MFs: <strong>{db_status["mf_count"]}</strong> · '
            f'Txns: <strong>{db_status["tx_count"]}</strong></div>'
            '</div>'
        )
        st.markdown(status_html, unsafe_allow_html=True)

        last_log = db_status.get("last_log")
        if last_log:
            st.caption(
                f"Last import: {last_log.get('source_type', '')} "
                f"({last_log.get('rows_inserted', 0)} rows) · {last_log.get('imported_at', '')}"
            )
        else:
            st.caption("No imports recorded yet.")

    return SidebarSelection(
        owner=owner_val,
        owner_label=owner_label,
        account_id=account_id,
        account_label=account_label,
        db_path=active_db,
        is_demo_mode=False,
        db_status=db_status,
    )
