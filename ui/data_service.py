"""Cached Streamlit data-access helpers wrapping `core/` analytics and DB queries."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, timedelta
import logging
from pathlib import Path
import threading
import time
from typing import Any

import streamlit as st

from core.analytics import (
    compare_mf_vs_benchmark,
    compare_portfolio_vs_benchmark,
    get_portfolio_snapshot,
)
from core.db import (
    get_connection,
    get_import_logs,
    query_holdings,
    query_mf_holdings,
    query_transactions,
)
from core.market_data import clear_market_cache

logger = logging.getLogger(__name__)

_BG_TTL_SECONDS = 300.0
_BG_LOCK = threading.Lock()
_BG_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bench-warm")
_BG_BENCH_RESULTS: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_BG_INFLIGHT_FUTURES: dict[tuple[Any, ...], Future[dict[str, Any]]] = {}
DEFAULT_WARMUP_BENCHMARK = "NIFTY_MIDCAP_150"


def get_data_version() -> int:
    return int(st.session_state.get("data_version", 0))


def invalidate_all_caches(clear_live_market: bool = False) -> None:
    """Invalidate Streamlit caches (and optionally the in-memory market TTL cache)."""
    if clear_live_market:
        clear_market_cache()
    with _BG_LOCK:
        _BG_BENCH_RESULTS.clear()
        _BG_INFLIGHT_FUTURES.clear()
    st.session_state["data_version"] = get_data_version() + 1
    st.cache_data.clear()


def _run_or_join_single_flight(
    bg_key: tuple[Any, ...],
    fn: Any,
    wait: bool,
) -> dict[str, Any] | None:
    """Share an in-flight Future or return fresh (<300s TTL) result; never duplicate concurrent work."""
    now = time.monotonic()
    with _BG_LOCK:
        cached = _BG_BENCH_RESULTS.get(bg_key)
        if cached is not None:
            ts, payload = cached
            if (now - ts) < _BG_TTL_SECONDS:
                return payload
            _BG_BENCH_RESULTS.pop(bg_key, None)

        fut = _BG_INFLIGHT_FUTURES.get(bg_key)
        if fut is None:
            def _wrapped() -> dict[str, Any]:
                try:
                    res = fn()
                    with _BG_LOCK:
                        _BG_BENCH_RESULTS[bg_key] = (time.monotonic(), res)
                    return res
                except Exception as exc:
                    logger.warning("Benchmark task failed for %s: %s", bg_key[:3], exc)
                    raise
                finally:
                    with _BG_LOCK:
                        _BG_INFLIGHT_FUTURES.pop(bg_key, None)

            fut = _BG_EXECUTOR.submit(_wrapped)
            _BG_INFLIGHT_FUTURES[bg_key] = fut

    if not wait:
        return None
    return fut.result(timeout=35)


def default_warmup_specs(
    owner: str | None,
    db_path: Path,
    version: int,
    ref_date: date | None = None,
) -> list[tuple[tuple[Any, ...], Any]]:
    """Canonical default 1Y keys shared between background warmup and Benchmarks tab."""
    today = ref_date or date.today()
    start_1y = (today - timedelta(days=365)).isoformat()
    end_iso = today.isoformat()
    db_str = str(db_path)

    return [
        (
            ("eq", owner, DEFAULT_WARMUP_BENCHMARK, start_1y, end_iso, (), True, db_str, version, None, None),
            lambda: compare_portfolio_vs_benchmark(
                owner=owner, benchmark_key=DEFAULT_WARMUP_BENCHMARK,
                start_date=start_1y, end_date=end_iso, adjust_for_splits=True, db_path=db_path,
            ),
        ),
        (
            ("eq", owner, DEFAULT_WARMUP_BENCHMARK, start_1y, end_iso, (), True, db_str, version, "India", None),
            lambda: compare_portfolio_vs_benchmark(
                owner=owner, benchmark_key=DEFAULT_WARMUP_BENCHMARK,
                start_date=start_1y, end_date=end_iso, adjust_for_splits=True, region="India", db_path=db_path,
            ),
        ),
        (
            ("mf", owner, DEFAULT_WARMUP_BENCHMARK, start_1y, end_iso, "ALL", db_str, version),
            lambda: compare_mf_vs_benchmark(
                owner=owner, benchmark_key=DEFAULT_WARMUP_BENCHMARK,
                start_date=start_1y, end_date=end_iso, scheme_filter="ALL", db_path=db_path,
            ),
        ),
    ]


def trigger_background_warmup(owner: str | None, db_path: Path, version: int) -> None:
    """Launch single-flight non-blocking warmup for default 1Y equity & MF benchmarks."""
    if not db_path.exists():
        return
    for bg_key, fn in default_warmup_specs(owner=owner, db_path=db_path, version=version):
        _run_or_join_single_flight(bg_key, fn, wait=False)


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
        include_xirr=False,
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
    bg_key = ("eq", owner, benchmark_key, start_date, end_date, exclude_symbols, adjust_for_splits, db_path_str, version, region, advisory_source)
    res = _run_or_join_single_flight(
        bg_key,
        lambda: compare_portfolio_vs_benchmark(
            owner=owner,
            benchmark_key=benchmark_key,
            start_date=start_date,
            end_date=end_date,
            exclude_symbols=list(exclude_symbols),
            adjust_for_splits=adjust_for_splits,
            region=region,
            advisory_source=advisory_source,
            db_path=Path(db_path_str),
        ),
        wait=True,
    )
    assert res is not None
    return res


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
    bg_key = ("mf", owner, benchmark_key, start_date, end_date, scheme_filter, db_path_str, version)
    res = _run_or_join_single_flight(
        bg_key,
        lambda: compare_mf_vs_benchmark(
            owner=owner,
            benchmark_key=benchmark_key,
            start_date=start_date,
            end_date=end_date,
            scheme_filter=scheme_filter,
            db_path=Path(db_path_str),
        ),
        wait=True,
    )
    assert res is not None
    return res


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
    """Collect lightweight DB stats using single SQL COUNT(*) queries."""
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    accounts_count = eq_count = mf_count = tx_count = aliases_count = 0
    if db_path.exists():
        with get_connection(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM accounts) AS acc_c,
                    (SELECT COUNT(*) FROM holdings) AS eq_c,
                    (SELECT COUNT(*) FROM mf_holdings) AS mf_c,
                    (SELECT COUNT(*) FROM transactions) AS tx_c,
                    (SELECT COUNT(*) FROM symbol_aliases) AS al_c
                """
            ).fetchone()
            if row:
                accounts_count, eq_count, mf_count, tx_count, aliases_count = (
                    int(row["acc_c"]),
                    int(row["eq_c"]),
                    int(row["mf_c"]),
                    int(row["tx_c"]),
                    int(row["al_c"]),
                )
    logs = get_import_logs(limit=1, db_path=db_path)
    last_log = logs[0] if logs else None
    return {
        "path": str(db_path),
        "size_bytes": size_bytes,
        "accounts_count": accounts_count,
        "eq_count": eq_count,
        "mf_count": mf_count,
        "tx_count": tx_count,
        "aliases_count": aliases_count,
        "last_log": last_log,
        "is_empty": (eq_count == 0 and mf_count == 0 and tx_count == 0),
    }
