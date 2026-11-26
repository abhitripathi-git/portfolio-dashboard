"""Cached Streamlit data-access helpers wrapping `core/` analytics and DB queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from core.analytics import (
    compare_mf_vs_benchmark,
    compare_portfolio_vs_benchmark,
    get_portfolio_snapshot,
)
from core.db import (
    get_import_logs,
    get_symbol_aliases,
    list_accounts,
    query_holdings,
    query_mf_holdings,
    query_transactions,
)
from core.market_data import clear_market_cache


def get_data_version() -> int:
    return int(st.session_state.get("data_version", 0))


def invalidate_all_caches(clear_live_market: bool = False) -> None:
    """Invalidate Streamlit caches (and optionally the in-memory market TTL cache)."""
    if clear_live_market:
        clear_market_cache()
    st.session_state["data_version"] = get_data_version() + 1
    st.cache_data.clear()


@st.cache_data(ttl=300, show_spinner=False)
def cached_snapshot(
    owner: str | None,
    include_mf: bool,
    db_path_str: str,
    version: int,
) -> dict[str, Any]:
    _ = version
    return get_portfolio_snapshot(
        owner=owner,
        include_mf=include_mf,
        db_path=Path(db_path_str),
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_benchmark_comparison(
    owner: str | None,
    benchmark_key: str,
    start_date: str | None,
    end_date: str | None,
    exclude_symbols: tuple[str, ...],
    adjust_for_splits: bool,
    db_path_str: str,
    version: int,
    region: str | None = None,
    advisory_source: str | None = None,
) -> dict[str, Any]:
    _ = version
    return compare_portfolio_vs_benchmark(
        owner=owner,
        benchmark_key=benchmark_key,
        start_date=start_date,
        end_date=end_date,
        exclude_symbols=list(exclude_symbols),
        adjust_for_splits=adjust_for_splits,
        region=region,
        advisory_source=advisory_source,
        db_path=Path(db_path_str),
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_mf_benchmark_comparison(
    owner: str | None,
    benchmark_key: str,
    start_date: str | None,
    end_date: str | None,
    scheme_filter: str | None,
    db_path_str: str,
    version: int,
) -> dict[str, Any]:
    _ = version
    return compare_mf_vs_benchmark(
        owner=owner,
        benchmark_key=benchmark_key,
        start_date=start_date,
        end_date=end_date,
        scheme_filter=scheme_filter,
        db_path=Path(db_path_str),
    )


@st.cache_data(ttl=300, show_spinner=False)
def _cached_eq_mf_rows(
    owner: str | None,
    account_id: int | None,
    db_path_str: str,
    version: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _ = version
    path = Path(db_path_str)
    return (
        query_holdings(owner=owner, account_id=account_id, db_path=path),
        query_mf_holdings(owner=owner, account_id=account_id, db_path=path),
    )


def get_enriched_snapshot(
    owner: str | None,
    account_id: int | None,
    db_path: Path,
) -> dict[str, Any]:
    """Return live snapshot enriched with `extra_data` and optionally filtered to `account_id`."""
    version = get_data_version()
    raw_snap = cached_snapshot(
        owner=owner,
        include_mf=True,
        db_path_str=str(db_path),
        version=version,
    )

    eq_db_rows, mf_db_rows = _cached_eq_mf_rows(
        owner=owner,
        account_id=account_id,
        db_path_str=str(db_path),
        version=version,
    )

    eq_extra_map: dict[tuple[Any, str], dict[str, Any]] = {}
    eq_meta_map: dict[tuple[Any, str], dict[str, Any]] = {}
    for r in eq_db_rows:
        key = (r.get("account_id"), str(r.get("symbol", "")).strip().upper())
        eq_extra_map[key] = r.get("extra_data") if isinstance(r.get("extra_data"), dict) else {}
        eq_meta_map[key] = {
            "broker": r.get("broker", ""),
            "exchange": r.get("exchange", "NSE"),
            "as_of_date": r.get("as_of_date", ""),
            "source": r.get("source", ""),
            "region": r.get("region", "India"),
            "advisory_source": r.get("advisory_source", "Self"),
            "advisory_notes": r.get("advisory_notes", ""),
        }

    mf_extra_map: dict[tuple[Any, str, str], dict[str, Any]] = {}
    mf_meta_map: dict[tuple[Any, str, str], dict[str, Any]] = {}
    for r in mf_db_rows:
        key = (
            r.get("account_id"),
            str(r.get("folio_number", "")).strip(),
            str(r.get("scheme_name", "")).strip(),
        )
        mf_extra_map[key] = r.get("extra_data") if isinstance(r.get("extra_data"), dict) else {}
        mf_meta_map[key] = {
            "broker": r.get("broker", ""),
            "as_of_date": r.get("as_of_date", ""),
            "source": r.get("source", ""),
            "region": r.get("region", "India"),
            "advisory_source": r.get("advisory_source", "Self"),
            "advisory_notes": r.get("advisory_notes", ""),
        }

    eq_out: list[dict[str, Any]] = []
    for item in raw_snap.get("equity_holdings", []):
        if account_id is not None and item.get("account_id") != account_id:
            continue
        key = (item.get("account_id"), str(item.get("symbol", "")).strip().upper())
        enriched = dict(item)
        enriched["extra_data"] = dict(eq_extra_map.get(key, {}))
        enriched.update(eq_meta_map.get(key, {}))
        eq_out.append(enriched)

    mf_out: list[dict[str, Any]] = []
    for item in raw_snap.get("mf_holdings", []):
        if account_id is not None and item.get("account_id") != account_id:
            continue
        key = (
            item.get("account_id"),
            str(item.get("folio_number", "")).strip(),
            str(item.get("scheme_name", "")).strip(),
        )
        enriched = dict(item)
        enriched["extra_data"] = dict(mf_extra_map.get(key, {}))
        enriched.update(mf_meta_map.get(key, {}))
        mf_out.append(enriched)

    if account_id is None:
        return {
            **raw_snap,
            "equity_holdings": eq_out,
            "mf_holdings": mf_out,
            "holdings": sorted(eq_out + mf_out, key=lambda x: x["current_value"], reverse=True),
        }

    # Recompute totals for single-account filter
    eq_inv = sum(x["invested_value"] for x in eq_out)
    eq_val = sum(x["current_value"] for x in eq_out)
    eq_day = sum(x["day_change"] for x in eq_out)
    mf_inv = sum(x["invested_value"] for x in mf_out)
    mf_val = sum(x["current_value"] for x in mf_out)
    tot_inv = eq_inv + mf_inv
    tot_val = eq_val + mf_val
    tot_pnl = tot_val - tot_inv
    tot_pnl_pct = (tot_pnl / tot_inv * 100.0) if tot_inv > 0 else 0.0
    prev_tot = tot_val - eq_day
    tot_day_pct = (eq_day / prev_tot * 100.0) if prev_tot > 0 else 0.0

    combined = eq_out + mf_out
    for item in combined:
        item["weight_pct"] = round(item["current_value"] / tot_val * 100.0, 2) if tot_val > 0 else 0.0
    combined.sort(key=lambda x: x["current_value"], reverse=True)

    return {
        "owner": raw_snap["owner"],
        "as_of_date": raw_snap["as_of_date"],
        "totals": {
            "invested": round(tot_inv, 2),
            "current_value": round(tot_val, 2),
            "pnl": round(tot_pnl, 2),
            "pnl_pct": round(tot_pnl_pct, 2),
            "day_change": round(eq_day, 2),
            "day_change_pct": round(tot_day_pct, 2),
            "xirr": None,
            "equity_xirr": None,
            "equity_invested": round(eq_inv, 2),
            "equity_current_value": round(eq_val, 2),
            "mf_invested": round(mf_inv, 2),
            "mf_current_value": round(mf_val, 2),
            "holdings_count": len(eq_out),
            "mf_count": len(mf_out),
        },
        "equity_holdings": eq_out,
        "mf_holdings": mf_out,
        "holdings": combined,
        "excluded_symbols": raw_snap.get("excluded_symbols", []),
        "warnings": raw_snap.get("warnings", []),
    }


def load_db_status(db_path: Path) -> dict[str, Any]:
    """Collect lightweight DB stats for the sidebar status card."""
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    accounts = list_accounts(db_path=db_path)
    eq_count = len(query_holdings(db_path=db_path))
    mf_count = len(query_mf_holdings(db_path=db_path))
    tx_count = len(query_transactions(db_path=db_path))
    aliases_count = len(get_symbol_aliases(db_path=db_path))
    logs = get_import_logs(limit=1, db_path=db_path)
    last_log = logs[0] if logs else None
    return {
        "path": str(db_path),
        "size_bytes": size_bytes,
        "accounts_count": len(accounts),
        "eq_count": eq_count,
        "mf_count": mf_count,
        "tx_count": tx_count,
        "aliases_count": aliases_count,
        "last_log": last_log,
        "is_empty": (eq_count == 0 and mf_count == 0 and tx_count == 0),
    }
