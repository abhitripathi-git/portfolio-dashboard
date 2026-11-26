"""FastMCP stdio server exposing personal Indian stock & mutual fund portfolio tools."""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from config import BENCHMARKS, DB_PATH as DEFAULT_DB_PATH
from core.analytics import compare_portfolio_vs_benchmark, get_portfolio_snapshot
from core.db import (
    init_db,
    list_accounts,
    query_mf_holdings,
    query_transactions,
    run_readonly_query,
    save_symbol_alias,
)
from core.market_data import fetch_mf_nav
from core.overlap import analyze_mf_stock_overlap
from ingestion.manual_delta import add_manual_trades

DB_PATH: Path = Path(os.environ.get("PORTFOLIO_DB_PATH") or DEFAULT_DB_PATH)

# Ensure SQLite schema and default family accounts exist on server startup.
init_db(db_path=DB_PATH, seed_default_owners=True)

mcp = FastMCP("personal-portfolio")


def _get_db_path() -> Path:
    return Path(DB_PATH)


def _normalize_owner_label(owner: str | None) -> str:
    if not owner or owner.strip().lower() in ("all", "combined", "both", "*"):
        return "Combined"
    return owner.strip().title()


def _normalize_benchmark_key(benchmark: str | None) -> str:
    if not benchmark or not benchmark.strip():
        return "NIFTY_MIDCAP_150"
    raw = benchmark.strip()
    upper_snake = raw.upper().replace("-", "_").replace(" ", "_")
    if upper_snake in BENCHMARKS:
        return upper_snake
    for key, cfg in BENCHMARKS.items():
        if cfg.get("label", "").strip().lower() == raw.lower():
            return key
    return raw


def _json_safe(value: Any) -> Any:
    """Recursively convert values into strict JSON-serializable Python primitives."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]

    # Handle numpy/pandas scalar types or Timestamp objects without requiring heavy imports
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            iso = value.isoformat()
            return None if iso == "NaT" else iso
        except Exception:
            pass
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if str(value) in {"NaN", "nan", "NaT", "<NA>"}:
        return None
    return str(value)


@mcp.tool()
def list_portfolio_accounts(owner: str | None = None) -> dict[str, Any]:
    """List portfolio brokerage/MF accounts stored in the local SQLite database.

    Args:
        owner: Optional family member filter: "Abhishek", "Tanu", or None for combined.

    Returns:
        Dictionary with owner filter, account count, and account metadata rows.
    """
    accounts = list_accounts(owner=owner, db_path=_get_db_path())
    return _json_safe(
        {
            "owner": _normalize_owner_label(owner),
            "count": len(accounts),
            "accounts": accounts,
        }
    )


@mcp.tool()
def get_portfolio_summary(
    owner: str | None = None,
    exclude_symbols: list[str] | None = None,
    include_mf: bool = True,
) -> dict[str, Any]:
    """Get a live portfolio summary (invested, current value, P&L, day change, XIRR).

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined family view.
        exclude_symbols: Optional list of stock symbols or MF codes/names to exclude
            for what-if simulation (e.g., ["RELIANCE", "TCS"]).
        include_mf: Whether to include mutual funds alongside direct equities.

    Returns:
        Live portfolio totals, XIRR metrics, top holdings, excluded symbols, and warnings.
    """
    snapshot = get_portfolio_snapshot(
        owner=owner,
        exclude_symbols=exclude_symbols,
        include_mf=include_mf,
        db_path=_get_db_path(),
    )
    return _json_safe(
        {
            "owner": snapshot["owner"],
            "as_of_date": snapshot["as_of_date"],
            "include_mf": include_mf,
            "excluded_symbols": snapshot["excluded_symbols"],
            "totals": snapshot["totals"],
            "top_holdings": snapshot["holdings"][:10],
            "warnings": snapshot["warnings"],
        }
    )


@mcp.tool()
def get_holdings_detail(
    owner: str | None = None,
    exclude_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Get detailed live valuation for all direct equity holdings.

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined.
        exclude_symbols: Optional list of stock symbols to exclude from the snapshot.

    Returns:
        Equity totals and per-stock holding rows with live price, P&L, and weights.
    """
    snapshot = get_portfolio_snapshot(
        owner=owner,
        exclude_symbols=exclude_symbols,
        include_mf=False,
        db_path=_get_db_path(),
    )
    return _json_safe(
        {
            "owner": snapshot["owner"],
            "as_of_date": snapshot["as_of_date"],
            "excluded_symbols": snapshot["excluded_symbols"],
            "count": len(snapshot["equity_holdings"]),
            "totals": snapshot["totals"],
            "holdings": snapshot["equity_holdings"],
            "warnings": snapshot["warnings"],
        }
    )


@mcp.tool()
def get_mf_holdings_detail(owner: str | None = None) -> dict[str, Any]:
    """Get detailed live valuation for all mutual fund holdings using AMFI/mftool NAVs.

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined.

    Returns:
        Mutual fund totals and per-scheme rows with units, avg NAV, latest NAV, and P&L.
    """
    db_path = _get_db_path()
    mf_rows = query_mf_holdings(owner=owner, db_path=db_path)
    warnings: list[str] = []

    nav_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for row in mf_rows:
        key = (str(row.get("amfi_code", "")).strip(), str(row.get("scheme_name", "")).strip())
        if key not in nav_cache:
            nav_cache[key] = fetch_mf_nav(amfi_code=key[0], scheme_name=key[1])

    mf_holdings: list[dict[str, Any]] = []
    mf_invested = 0.0
    mf_current_value = 0.0

    for row in mf_rows:
        units = float(row.get("units", 0.0))
        avg_nav = float(row.get("avg_nav", 0.0))
        invested = float(row.get("invested_value") or round(units * avg_nav, 2))
        key = (str(row.get("amfi_code", "")).strip(), str(row.get("scheme_name", "")).strip())
        nav_info = nav_cache.get(key)

        if nav_info and nav_info.get("nav") is not None:
            curr_nav = float(nav_info["nav"])
            curr_val = units * curr_nav
            price_missing = False
            resolved_code = nav_info.get("scheme_code") or key[0]
            nav_date = nav_info.get("last_updated", "")
        else:
            curr_nav = None
            curr_val = invested
            price_missing = True
            resolved_code = key[0]
            nav_date = ""
            warnings.append(f"Latest NAV unavailable for '{key[1]}'; valued at cost.")

        pnl = curr_val - invested
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0
        mf_invested += invested
        mf_current_value += curr_val

        mf_holdings.append(
            {
                "asset_class": "MF",
                "owner": row.get("owner", ""),
                "account_id": row.get("account_id"),
                "account_name": row.get("account_name", ""),
                "scheme_name": key[1],
                "amfi_code": resolved_code,
                "folio_number": row.get("folio_number", ""),
                "isin": row.get("isin", ""),
                "units": round(units, 6),
                "avg_nav": round(avg_nav, 4),
                "invested_value": round(invested, 2),
                "current_nav": round(curr_nav, 4) if curr_nav is not None else None,
                "current_value": round(curr_val, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "weight_pct": 0.0,
                "price_missing": price_missing,
                "nav_date": nav_date,
                "currency": "INR",
            }
        )

    for item in mf_holdings:
        item["weight_pct"] = (
            round(item["current_value"] / mf_current_value * 100.0, 2)
            if mf_current_value > 0
            else 0.0
        )
    mf_holdings.sort(key=lambda x: x["current_value"], reverse=True)

    mf_pnl = mf_current_value - mf_invested
    mf_pnl_pct = (mf_pnl / mf_invested * 100.0) if mf_invested > 0 else 0.0

    return _json_safe(
        {
            "owner": _normalize_owner_label(owner),
            "as_of_date": date.today().isoformat(),
            "count": len(mf_holdings),
            "totals": {
                "invested": round(mf_invested, 2),
                "current_value": round(mf_current_value, 2),
                "pnl": round(mf_pnl, 2),
                "pnl_pct": round(mf_pnl_pct, 2),
            },
            "mf_holdings": mf_holdings,
            "warnings": list(dict.fromkeys(warnings)),
        }
    )


@mcp.tool()
def get_transactions_history(
    owner: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Query recorded buy/sell/dividend/bonus/split transactions with optional filters.

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined.
        start_date: Inclusive start date in YYYY-MM-DD format.
        end_date: Inclusive end date in YYYY-MM-DD format.
        symbol: Optional stock symbol filter (e.g., "RELIANCE").

    Returns:
        Filter summary, transaction count, and chronological list of transactions.
    """
    txs = query_transactions(
        owner=owner,
        start_date=start_date,
        end_date=end_date,
        symbol=symbol,
        db_path=_get_db_path(),
    )
    return _json_safe(
        {
            "owner": _normalize_owner_label(owner),
            "start_date": start_date,
            "end_date": end_date,
            "symbol": symbol.strip().upper() if symbol else None,
            "count": len(txs),
            "transactions": txs,
        }
    )


@mcp.tool()
def compare_against_benchmark(
    owner: str | None = None,
    benchmark: str = "NIFTY_MIDCAP_150",
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_symbols: list[str] | None = None,
    region: str | None = None,
    advisory_source: str | None = None,
) -> dict[str, Any]:
    """Compare equity portfolio returns (TWR, XIRR, simple return) vs an Indian benchmark.

    Supports excluding specific scrips (what-if simulation), region filtering
    ("India" or "Foreign"), advisory filtering (e.g., "Self", "RC Capital",
    "Fintrekk Capital"), and custom date ranges.

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined.
        benchmark: Benchmark key or name. Available keys: "NIFTY_50",
            "NIFTY_MIDCAP_150", "NIFTY_MIDCAP_50", "NIFTY_SMALLCAP_250",
            "NIFTY_NEXT_50", "SENSEX".
        start_date: Optional start date (YYYY-MM-DD). Defaults to earliest trade or 1Y ago.
        end_date: Optional end date (YYYY-MM-DD). Defaults to today.
        exclude_symbols: Optional list of stock symbols to exclude before comparing
            (e.g., ["RELIANCE", "TCS", "INFY"]).
        region: Optional region filter ("India" or "Foreign").
        advisory_source: Optional advisory filter (e.g., "Self", "RC Capital", "Fintrekk Capital").

    Returns:
        Summary metrics (portfolio vs benchmark TWR, XIRR, alpha), daily timeseries,
        included/excluded symbols, and any warnings.
    """
    benchmark_key = _normalize_benchmark_key(benchmark)
    result = compare_portfolio_vs_benchmark(
        owner=owner,
        benchmark_key=benchmark_key,
        start_date=start_date,
        end_date=end_date,
        exclude_symbols=exclude_symbols,
        region=region,
        advisory_source=advisory_source,
        db_path=_get_db_path(),
    )
    return _json_safe(result)


@mcp.tool()
def check_mf_stock_overlap(owner: str | None = None) -> dict[str, Any]:
    """Analyze overlap between direct equity holdings and mutual fund schemes.

    Args:
        owner: Optional owner filter: "Abhishek", "Tanu", or None for combined.

    Returns:
        Overlap summary, direct-vs-MF overlapping stocks, MF-vs-MF overlap matrix,
        effective combined stock exposure, MF constituent provenance, and warnings.
    """
    result = analyze_mf_stock_overlap(owner=owner, db_path=_get_db_path())
    return _json_safe(result)


@mcp.tool()
def add_trade_delta(
    trades_text: str,
    owner: str = "Abhishek",
    broker: str = "5paisa",
    rebuild_holdings: bool = True,
) -> dict[str, Any]:
    """Parse and insert new trade deltas (shorthand lines or pasted CSV/TSV) into SQLite.

    Deduplicates automatically so re-entering overlapping trades is safe.

    Args:
        trades_text: Newline-separated trades such as
            "2026-11-20 BUY RELIANCE 10 @ 2950" or "2026-11-21 SELL TCS 5 @ 4100",
            or pasted CSV/TSV broker rows.
        owner: Account owner ("Abhishek" or "Tanu").
        broker: Broker name (default "5paisa").
        rebuild_holdings: Recompute net equity holdings from all transactions after insert.

    Returns:
        Account info, parsed count, inserted/skipped counts, and rebuilt holdings count.
    """
    result = add_manual_trades(
        text=trades_text,
        owner=owner,
        broker=broker,
        rebuild_holdings=rebuild_holdings,
        db_path=_get_db_path(),
    )
    return _json_safe(result)


@mcp.tool()
def add_symbol_mapping(
    raw_symbol: str,
    yahoo_symbol: str,
    isin: str = "",
    company_name: str = "",
    sector: str = "",
) -> dict[str, Any]:
    """Save or update a broker/raw stock symbol mapping to a Yahoo Finance ticker.

    Args:
        raw_symbol: Broker symbol as stored in transactions/holdings (e.g., "M&M").
        yahoo_symbol: Yahoo Finance ticker (e.g., "M&M.NS" or "500520.BO").
        isin: Optional 12-character ISIN (e.g., "INE101A01026").
        company_name: Optional human-readable company name.
        sector: Optional sector label.

    Returns:
        Confirmation dictionary with the persisted mapping.
    """
    db_path = _get_db_path()
    init_db(db_path=db_path, seed_default_owners=False)
    save_symbol_alias(
        raw_symbol=raw_symbol,
        yahoo_symbol=yahoo_symbol,
        isin=isin,
        company_name=company_name,
        sector=sector,
        db_path=db_path,
    )
    return _json_safe(
        {
            "status": "saved",
            "raw_symbol": raw_symbol.strip().upper(),
            "yahoo_symbol": yahoo_symbol.strip(),
            "isin": isin.strip().upper(),
            "company_name": company_name,
            "sector": sector,
        }
    )


@mcp.tool()
def run_readonly_sql(sql: str) -> dict[str, Any]:
    """Run a safe read-only SQL query (SELECT, WITH, or PRAGMA) against portfolio SQLite.

    Mutating statements (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, ATTACH)
    are strictly blocked.

    Database schema reference:
    - accounts(id, owner, broker, account_name, account_code, extra_data, created_at)
    - transactions(id, account_id, trade_date, symbol, isin, exchange, asset_type,
      side ['BUY'|'SELL'|'DIVIDEND'|'BONUS'|'SPLIT'], quantity, price, amount, fees,
      source, region, advisory_source, advisory_notes, dedup_hash, extra_data, created_at)
    - holdings(id, account_id, symbol, isin, company_name, exchange, asset_type,
      quantity, avg_price, invested_value, as_of_date, source, region, advisory_source,
      advisory_notes, extra_data, updated_at)
    - holding_baselines(id, account_id, symbol, isin, company_name, exchange, asset_type,
      quantity, avg_price, invested_value, as_of_date, source, region, advisory_source,
      advisory_notes, extra_data, updated_at)
    - mf_holdings(id, account_id, folio_number, scheme_name, amfi_code, isin,
      units, avg_nav, invested_value, as_of_date, source, region, advisory_source,
      advisory_notes, extra_data, updated_at)
    - other_assets(id, owner, name, category, invested_value, current_value,
      as_of_date, notes, extra_data, updated_at)
    - symbol_aliases(raw_symbol, yahoo_symbol, isin, company_name, sector, updated_at)
    - import_logs(id, account_id, source_type, filename, rows_inserted, rows_skipped,
      notes, imported_at)

    Note: `extra_data` columns hold JSON objects (parseable with SQLite `json_extract`).

    Args:
        sql: A read-only SELECT, WITH, or PRAGMA query.

    Returns:
        Dictionary with `row_count` and `rows` (list of row dicts).
    """
    rows = run_readonly_query(sql=sql, db_path=_get_db_path())
    return _json_safe({"row_count": len(rows), "rows": rows})


if __name__ == "__main__":
    mcp.run()
