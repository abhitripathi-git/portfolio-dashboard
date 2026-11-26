"""Quantitative portfolio analytics: XIRR, live snapshots, and benchmark comparison."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from config import BENCHMARKS
from core.db import (
    query_holding_baselines,
    query_holdings,
    query_mf_holdings,
    query_transactions,
)
from core.market_data import (
    fetch_benchmark_series,
    fetch_historical_prices,
    fetch_latest_prices,
    fetch_mf_historical_nav,
    fetch_mf_nav,
    fetch_stock_splits,
    fetch_usdinr_rate,
)


def _to_date(val: date | datetime | str) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return pd.Timestamp(val).date()


def compute_xirr(cashflows: list[tuple[date | datetime | str, float]]) -> float | None:
    """Compute annualized XIRR using Newton-Raphson with bisection fallback.

    Args:
        cashflows: Sequence of `(date, amount)` pairs where outflows (buys/investments)
            are negative and inflows (sells/dividends/current value) are positive.

    Returns:
        Annualized decimal return rate (e.g. `0.154` for 15.4%), or `None` if invalid.
    """
    cleaned: list[tuple[date, float]] = []
    for dt_raw, amt_raw in cashflows:
        try:
            amt = float(amt_raw)
            if math.isnan(amt) or math.isinf(amt) or abs(amt) <= 1e-12:
                continue
            cleaned.append((_to_date(dt_raw), amt))
        except Exception:
            continue

    if len(cleaned) < 2:
        return None

    has_pos = any(amt > 0 for _, amt in cleaned)
    has_neg = any(amt < 0 for _, amt in cleaned)
    if not (has_pos and has_neg):
        return None

    cleaned.sort(key=lambda x: x[0])
    d0 = cleaned[0][0]
    dn = cleaned[-1][0]
    if dn <= d0:
        return None

    dated_terms = [((d - d0).days / 365.0, amt) for d, amt in cleaned]

    def npv(rate: float) -> float:
        if rate <= -1.0:
            return float("inf")
        total = 0.0
        log_base = math.log1p(rate)
        for t, amt in dated_terms:
            exp_arg = -t * log_base
            if exp_arg > 700:
                return float("inf") if amt > 0 else float("-inf")
            if exp_arg < -700:
                continue
            total += amt * math.exp(exp_arg)
        return total

    def d_npv(rate: float) -> float:
        if rate <= -1.0:
            return float("nan")
        total = 0.0
        log_base = math.log1p(rate)
        one_plus_r = 1.0 + rate
        for t, amt in dated_terms:
            if t == 0.0:
                continue
            exp_arg = -t * log_base
            if exp_arg > 700 or exp_arg < -700:
                continue
            total += -t * amt * math.exp(exp_arg) / one_plus_r
        return total

    # 1. Newton-Raphson from multiple candidate starting points
    for guess in (0.1, 0.0, 0.25, -0.2, 0.5, -0.5, 1.0, -0.8):
        r = guess
        for _ in range(100):
            if r <= -0.999999 or r > 100.0:
                break
            val = npv(r)
            if not math.isfinite(val):
                break
            if abs(val) < 1e-7:
                return float(r)
            deriv = d_npv(r)
            if not math.isfinite(deriv) or abs(deriv) < 1e-12:
                break
            step = val / deriv
            next_r = r - step
            if next_r <= -1.0:
                next_r = -0.999
            if abs(next_r - r) < 1e-9 and abs(npv(next_r)) < 1e-5:
                return float(next_r)
            r = next_r

    # 2. Bisection fallback across candidate brackets
    grid = [
        -0.999, -0.99, -0.95, -0.9, -0.8, -0.7, -0.5, -0.3, -0.1,
        0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0,
    ]
    roots: list[float] = []
    for i in range(len(grid) - 1):
        lo, hi = grid[i], grid[i + 1]
        flo = npv(lo)
        fhi = npv(hi)
        if not (math.isfinite(flo) and math.isfinite(fhi)):
            continue
        if abs(flo) < 1e-7:
            roots.append(lo)
            continue
        if flo * fhi < 0:
            a, b, fa = lo, hi, flo
            for _ in range(120):
                mid = 0.5 * (a + b)
                fmid = npv(mid)
                if not math.isfinite(fmid):
                    break
                if abs(fmid) < 1e-8 or (b - a) < 1e-10:
                    roots.append(mid)
                    break
                if fa * fmid < 0:
                    b = mid
                else:
                    a, fa = mid, fmid

    if roots:
        # Pick the financially most plausible root (closest to 10% CAGR)
        best = min(roots, key=lambda r: abs(r - 0.1))
        return float(best)

    return None


def _compute_portfolio_xirr_from_transactions(
    owner: str | None,
    excluded: set[str],
    include_mf: bool,
    equity_current_value: float,
    mf_current_value: float,
    as_of: date,
    db_path: Path | str | None,
) -> tuple[float | None, float | None, list[str]]:
    """Compute (overall_xirr, equity_xirr, notes) from DB transactions."""
    warnings: list[str] = []
    txs = query_transactions(owner=owner, db_path=db_path)
    if not txs:
        return None, None, warnings

    eq_cfs: list[tuple[date, float]] = []
    mf_cfs: list[tuple[date, float]] = []
    tx_fx: float | None = None

    for tx in txs:
        sym = str(tx.get("symbol", "")).strip().upper()
        if sym in excluded:
            continue
        asset_type = str(tx.get("asset_type", "EQUITY")).strip().upper()
        side = str(tx.get("side", "")).strip().upper()
        amt = float(tx.get("amount") or (float(tx.get("quantity", 0.0)) * float(tx.get("price", 0.0))))
        fees = float(tx.get("fees") or 0.0)
        extra = tx.get("extra_data") if isinstance(tx.get("extra_data"), dict) else {}
        if str(extra.get("currency") or "INR").strip().upper() == "USD":
            if tx_fx is None:
                fetched_fx = fetch_usdinr_rate()
                tx_fx = fetched_fx if (fetched_fx is not None and fetched_fx > 0) else 85.0
            amt *= tx_fx
            fees *= tx_fx
        dt = _to_date(tx["trade_date"])

        if side == "BUY":
            cf = -(amt + fees)
        elif side in ("SELL", "DIVIDEND"):
            cf = max(amt - fees, 0.0) if side == "SELL" else amt
        else:
            continue

        if asset_type == "MF":
            mf_cfs.append((dt, cf))
        else:
            eq_cfs.append((dt, cf))

    equity_xirr: float | None = None
    if eq_cfs and equity_current_value >= 0:
        equity_xirr = compute_xirr(eq_cfs + [(as_of, equity_current_value)])

    overall_xirr = equity_xirr
    if include_mf and mf_cfs:
        combined_cfs = eq_cfs + mf_cfs + [(as_of, equity_current_value + mf_current_value)]
        overall_xirr = compute_xirr(combined_cfs)
    elif include_mf and mf_current_value > 0 and not mf_cfs and eq_cfs:
        warnings.append("MF transaction history unavailable; XIRR reflects equity cashflows only.")

    return overall_xirr, equity_xirr, warnings


def get_portfolio_snapshot(
    owner: str | None = None,
    exclude_symbols: list[str] | None = None,
    include_mf: bool = True,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build a live portfolio snapshot for an owner (or combined) with on-demand pricing."""
    excluded_list = sorted({s.strip().upper() for s in (exclude_symbols or []) if s and s.strip()})
    excluded_set = set(excluded_list)
    warnings: list[str] = []

    eq_rows = query_holdings(owner=owner, exclude_symbols=excluded_list, db_path=db_path)
    mf_rows = query_mf_holdings(owner=owner, db_path=db_path) if include_mf else []
    if mf_rows and excluded_set:
        mf_rows = [
            m for m in mf_rows
            if str(m.get("amfi_code", "")).strip().upper() not in excluded_set
            and str(m.get("scheme_name", "")).strip().upper() not in excluded_set
        ]

    unique_eq_symbols = sorted(
        {
            str(r["symbol"]).strip().upper()
            for r in eq_rows
            if r.get("symbol")
            and not (isinstance(r.get("extra_data"), dict) and r["extra_data"].get("manual_ltp") is not None)
        }
    )
    unique_mf_keys: list[tuple[str, str]] = list(
        dict.fromkeys(
            (str(m.get("amfi_code", "")).strip(), str(m.get("scheme_name", "")).strip())
            for m in mf_rows
            if not (isinstance(m.get("extra_data"), dict) and m["extra_data"].get("manual_ltp") is not None)
        )
    )

    latest_prices: dict[str, dict[str, Any]] = {}
    mf_nav_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    if unique_eq_symbols or unique_mf_keys:
        with ThreadPoolExecutor(max_workers=8) as pool:
            eq_fut = (
                pool.submit(fetch_latest_prices, unique_eq_symbols, db_path=db_path)
                if unique_eq_symbols
                else None
            )
            mf_futs = {
                key: pool.submit(fetch_mf_nav, amfi_code=key[0], scheme_name=key[1])
                for key in unique_mf_keys
            }
            if eq_fut is not None:
                latest_prices = eq_fut.result()
            for key, fut in mf_futs.items():
                mf_nav_cache[key] = fut.result()

    equity_holdings: list[dict[str, Any]] = []
    equity_invested = 0.0
    equity_current_value = 0.0
    equity_day_change = 0.0
    usdinr_rate: float | None = None

    def _get_fx() -> float:
        nonlocal usdinr_rate
        if usdinr_rate is None:
            fetched = fetch_usdinr_rate()
            if fetched is not None and fetched > 0:
                usdinr_rate = fetched
            else:
                usdinr_rate = 85.0
                warnings.append("Live USD/INR FX unavailable; used fallback ₹85.00/USD.")
        return usdinr_rate

    for row in eq_rows:
        sym = str(row["symbol"]).strip().upper()
        qty = float(row.get("quantity", 0.0))
        extra = row.get("extra_data") if isinstance(row.get("extra_data"), dict) else {}
        cost_ccy = str(extra.get("cost_currency") or extra.get("currency") or "INR").strip().upper()
        avg_price = float(row.get("avg_price", 0.0))
        invested = float(row.get("invested_value") or round(qty * avg_price, 2))
        if cost_ccy == "USD":
            fx = _get_fx()
            avg_price *= fx
            invested *= fx

        pinfo = latest_prices.get(sym, {})
        live_price = pinfo.get("price")
        prev_close = pinfo.get("prev_close")
        live_ccy = str(pinfo.get("currency", "INR")).strip().upper()

        if live_price is None and extra.get("manual_ltp") is not None:
            try:
                live_price = float(extra["manual_ltp"])
                prev_close = live_price
                live_ccy = str(
                    extra.get("live_currency") or extra.get("currency") or cost_ccy or "INR"
                ).strip().upper()
            except (TypeError, ValueError):
                live_price = None

        if live_price is not None:
            curr_price = float(live_price)
            prev = float(prev_close) if prev_close is not None else curr_price
            if live_ccy == "USD":
                fx = _get_fx()
                curr_price *= fx
                prev *= fx
            curr_val = qty * curr_price
            day_chg = qty * (curr_price - prev)
            day_chg_pct = float(pinfo.get("change_pct") or 0.0)
            price_missing = False
        else:
            curr_price = None
            curr_val = invested
            day_chg = 0.0
            day_chg_pct = 0.0
            price_missing = True
            warnings.append(
                f"Live price unavailable for {sym} ({pinfo.get('yahoo_symbol', sym)}); valued at cost."
            )

        pnl = curr_val - invested
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0

        equity_invested += invested
        equity_current_value += curr_val
        equity_day_change += day_chg

        equity_holdings.append(
            {
                "asset_class": "EQUITY",
                "asset_type": str(row.get("asset_type") or "EQUITY").strip().upper(),
                "owner": row.get("owner", ""),
                "account_id": row.get("account_id"),
                "account_name": row.get("account_name", ""),
                "symbol": sym,
                "yahoo_symbol": pinfo.get("yahoo_symbol", sym),
                "isin": row.get("isin", ""),
                "company_name": row.get("company_name") or sym,
                "region": str(row.get("region") or "India").strip(),
                "advisory_source": str(row.get("advisory_source") or "Self").strip(),
                "advisory_notes": str(row.get("advisory_notes") or "").strip(),
                "quantity": round(qty, 6),
                "avg_price": round(avg_price, 4),
                "invested_value": round(invested, 2),
                "current_price": round(curr_price, 4) if curr_price is not None else None,
                "prev_close": round(float(prev_close), 4) if prev_close is not None else None,
                "current_value": round(curr_val, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "day_change": round(day_chg, 2),
                "day_change_pct": round(day_chg_pct, 2),
                "weight_pct": 0.0,
                "price_missing": price_missing,
                "currency": "INR",
            }
        )

    mf_holdings_out: list[dict[str, Any]] = []
    mf_invested = 0.0
    mf_current_value = 0.0

    for m in mf_rows:
        units = float(m.get("units", 0.0))
        extra = m.get("extra_data") if isinstance(m.get("extra_data"), dict) else {}
        cost_ccy = str(extra.get("cost_currency") or extra.get("currency") or "INR").strip().upper()
        avg_nav = float(m.get("avg_nav", 0.0))
        invested = float(m.get("invested_value") or round(units * avg_nav, 2))
        if cost_ccy == "USD":
            fx = _get_fx()
            avg_nav *= fx
            invested *= fx

        key = (str(m.get("amfi_code", "")).strip(), str(m.get("scheme_name", "")).strip())
        nav_info = mf_nav_cache.get(key)
        live_nav_raw = nav_info.get("nav") if (nav_info and nav_info.get("nav") is not None) else None
        live_ccy = "INR"
        if live_nav_raw is None and extra.get("manual_ltp") is not None:
            try:
                live_nav_raw = float(extra["manual_ltp"])
                live_ccy = str(
                    extra.get("live_currency") or extra.get("currency") or cost_ccy or "INR"
                ).strip().upper()
            except (TypeError, ValueError):
                live_nav_raw = None

        if live_nav_raw is not None:
            curr_nav = float(live_nav_raw)
            if live_ccy == "USD":
                curr_nav *= _get_fx()
            curr_val = units * curr_nav
            price_missing = False
            resolved_code = (nav_info.get("scheme_code") if nav_info else None) or key[0]
            nav_date = (nav_info.get("last_updated") if nav_info else None) or str(extra.get("nav_date") or "")
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

        mf_holdings_out.append(
            {
                "asset_class": "MF",
                "owner": m.get("owner", ""),
                "account_id": m.get("account_id"),
                "account_name": m.get("account_name", ""),
                "symbol": resolved_code or key[1],
                "scheme_name": key[1],
                "amfi_code": resolved_code,
                "folio_number": m.get("folio_number", ""),
                "isin": m.get("isin", ""),
                "company_name": key[1],
                "region": str(m.get("region") or "India").strip(),
                "advisory_source": str(m.get("advisory_source") or "Self").strip(),
                "advisory_notes": str(m.get("advisory_notes") or "").strip(),
                "quantity": round(units, 6),
                "avg_price": round(avg_nav, 4),
                "invested_value": round(invested, 2),
                "current_price": round(curr_nav, 4) if curr_nav is not None else None,
                "current_value": round(curr_val, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "day_change": 0.0,
                "day_change_pct": 0.0,
                "weight_pct": 0.0,
                "price_missing": price_missing,
                "nav_date": nav_date,
                "currency": "INR",
            }
        )

    total_invested = equity_invested + mf_invested
    total_current = equity_current_value + mf_current_value
    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100.0) if total_invested > 0 else 0.0
    prev_total = total_current - equity_day_change
    total_day_change_pct = (equity_day_change / prev_total * 100.0) if prev_total > 0 else 0.0

    combined = equity_holdings + mf_holdings_out
    for item in combined:
        item["weight_pct"] = (
            round(item["current_value"] / total_current * 100.0, 2) if total_current > 0 else 0.0
        )

    combined.sort(key=lambda x: x["current_value"], reverse=True)
    equity_holdings.sort(key=lambda x: x["current_value"], reverse=True)
    mf_holdings_out.sort(key=lambda x: x["current_value"], reverse=True)

    today = date.today()
    overall_xirr, equity_xirr, xirr_notes = _compute_portfolio_xirr_from_transactions(
        owner=owner,
        excluded=excluded_set,
        include_mf=include_mf,
        equity_current_value=equity_current_value,
        mf_current_value=mf_current_value,
        as_of=today,
        db_path=db_path,
    )
    warnings.extend(xirr_notes)

    return {
        "owner": owner.strip().title() if owner and owner.strip().lower() not in ("all", "combined", "both", "*") else "Combined",
        "as_of_date": today.isoformat(),
        "totals": {
            "invested": round(total_invested, 2),
            "current_value": round(total_current, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl_pct, 2),
            "day_change": round(equity_day_change, 2),
            "day_change_pct": round(total_day_change_pct, 2),
            "xirr": round(overall_xirr, 6) if overall_xirr is not None else None,
            "equity_xirr": round(equity_xirr, 6) if equity_xirr is not None else None,
            "equity_invested": round(equity_invested, 2),
            "equity_current_value": round(equity_current_value, 2),
            "mf_invested": round(mf_invested, 2),
            "mf_current_value": round(mf_current_value, 2),
            "holdings_count": len(equity_holdings),
            "mf_count": len(mf_holdings_out),
        },
        "equity_holdings": equity_holdings,
        "mf_holdings": mf_holdings_out,
        "holdings": combined,
        "excluded_symbols": excluded_list,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _empty_benchmark_response(
    owner_label: str,
    mode: str,
    benchmark_key: str,
    benchmark_label: str,
    start_iso: str,
    end_iso: str,
    excluded_list: list[str],
    included_symbols: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "summary": {
            "owner": owner_label,
            "mode": mode,
            "benchmark_key": benchmark_key,
            "benchmark_label": benchmark_label,
            "start_date": start_iso,
            "end_date": end_iso,
            "portfolio_start_value": 0.0,
            "portfolio_end_value": 0.0,
            "net_invested": 0.0,
            "portfolio_simple_return_pct": None,
            "portfolio_twr_pct": None,
            "portfolio_xirr": None,
            "benchmark_matched_value": None,
            "benchmark_simple_return_pct": None,
            "benchmark_twr_pct": None,
            "benchmark_xirr": None,
            "alpha_twr_pct": None,
            "alpha_xirr_pct": None,
        },
        "timeseries": [],
        "included_symbols": included_symbols,
        "excluded_symbols": excluded_list,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _build_split_multiplier_fn(
    symbols: list[str],
    earliest_date: str,
    adjust_for_splits: bool,
    db_path: Path | str | None,
) -> dict[str, pd.Series]:
    if not adjust_for_splits or not symbols:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    max_workers = min(10, len(symbols))
    splits_map: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            sym: pool.submit(fetch_stock_splits, sym, earliest_date, db_path)
            for sym in symbols
        }
        for sym, fut in futures.items():
            try:
                splits_map[sym] = fut.result()
            except Exception:
                splits_map[sym] = pd.Series(dtype=float)
    return splits_map


def _future_split_factor(splits: pd.Series | None, tx_date: date) -> float:
    if splits is None or splits.empty:
        return 1.0
    ts = pd.Timestamp(tx_date)
    later = splits[splits.index > ts]
    if later.empty:
        return 1.0
    factor = float(later.prod())
    return factor if factor > 0 else 1.0


def _has_matching_yf_split(splits: pd.Series | None, tx_date: date, window_days: int = 10) -> bool:
    if splits is None or splits.empty:
        return False
    ts = pd.Timestamp(tx_date)
    diff = (splits.index - ts).days
    return bool(((diff >= -window_days) & (diff <= window_days)).any())


def compare_portfolio_vs_benchmark(
    owner: str | None = None,
    benchmark_key: str = "NIFTY_MIDCAP_150",
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_symbols: list[str] | None = None,
    adjust_for_splits: bool = True,
    region: str | None = None,
    advisory_source: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Compare equity portfolio returns vs. a benchmark index over `[start_date, end_date]`.

    Uses baseline-aware transaction replay (with split-adjusted tradebook quantities
    matching Yahoo `auto_adjust=True` prices) when transactions exist, or falls back
    to backtesting current holdings if no transactions are recorded.
    """
    owner_label = (
        owner.strip().title()
        if owner and owner.strip().lower() not in ("all", "combined", "both", "*")
        else "Combined"
    )
    excluded_list = sorted({s.strip().upper() for s in (exclude_symbols or []) if s and s.strip()})
    excluded_set = set(excluded_list)
    region_filter = region.strip().lower() if region and region.strip().lower() not in ("all", "combined", "") else None
    adv_filter = advisory_source.strip().lower() if advisory_source and advisory_source.strip().lower() not in ("all", "combined", "") else None

    benchmark_label = BENCHMARKS.get(benchmark_key, {}).get("label", benchmark_key)
    end_iso = pd.Timestamp(end_date).date().isoformat() if end_date else date.today().isoformat()
    warnings: list[str] = []

    all_baselines = query_holding_baselines(owner=owner, db_path=db_path)
    all_holdings = query_holdings(owner=owner, exclude_symbols=excluded_list, db_path=db_path)

    sym_meta: dict[str, tuple[str, str]] = {}
    for src_row in [*all_holdings, *all_baselines]:
        sym = str(src_row.get("symbol", "")).strip().upper()
        if sym:
            sym_meta[sym] = (
                str(src_row.get("region") or "India").strip(),
                str(src_row.get("advisory_source") or "Self").strip(),
            )

    def _matches_segment(sym: str, row_region: Any, row_adv: Any) -> bool:
        meta_reg, meta_adv = sym_meta.get(sym, ("India", "Self"))
        eff_reg = str(row_region or meta_reg or "India").strip()
        if eff_reg == "India" and meta_reg != "India":
            eff_reg = meta_reg
        eff_adv = str(row_adv or meta_adv or "Self").strip()
        if eff_adv == "Self" and meta_adv != "Self":
            eff_adv = meta_adv
        if region_filter and eff_reg.lower() != region_filter:
            return False
        if adv_filter and eff_adv.lower() != adv_filter:
            return False
        return True

    filtered_baselines = [
        b for b in all_baselines
        if str(b.get("asset_type", "EQUITY")).strip().upper() != "MF"
        and str(b.get("symbol", "")).strip().upper() not in excluded_set
        and _matches_segment(
            str(b.get("symbol", "")).strip().upper(),
            b.get("region"),
            b.get("advisory_source"),
        )
    ]
    baseline_qty_map: dict[str, float] = {}
    max_baseline_date: date | None = None
    for b in filtered_baselines:
        sym = str(b["symbol"]).strip().upper()
        baseline_qty_map[sym] = baseline_qty_map.get(sym, 0.0) + float(b.get("quantity", 0.0))
        b_dt = _to_date(b["as_of_date"])
        if max_baseline_date is None or b_dt > max_baseline_date:
            max_baseline_date = b_dt

    raw_txs = query_transactions(owner=owner, end_date=end_iso, db_path=db_path)
    eq_txs = [
        tx for tx in raw_txs
        if str(tx.get("asset_type", "EQUITY")).strip().upper() != "MF"
        and str(tx.get("symbol", "")).strip().upper() not in excluded_set
        and _matches_segment(
            str(tx.get("symbol", "")).strip().upper(),
            tx.get("region"),
            tx.get("advisory_source"),
        )
    ]

    if eq_txs:
        mode = "transactions"
        earliest_tx_date = min(str(tx["trade_date"]) for tx in eq_txs)
        start_iso = pd.Timestamp(start_date).date().isoformat() if start_date else earliest_tx_date
        if start_iso > end_iso:
            warnings.append(f"start_date ({start_iso}) is after end_date ({end_iso}).")
            return _empty_benchmark_response(
                owner_label, mode, benchmark_key, benchmark_label, start_iso, end_iso,
                excluded_list, [], warnings,
            )

        included_symbols = sorted(
            {str(tx["symbol"]).strip().upper() for tx in eq_txs}
            | {sym for sym, q in baseline_qty_map.items() if q > 1e-9}
        )
        split_since = min(earliest_tx_date, start_iso)
        splits_map = _build_split_multiplier_fn(included_symbols, split_since, adjust_for_splits, db_path)

        # Normalize transactions to adjusted share basis
        norm_txs: list[dict[str, Any]] = []
        for tx in eq_txs:
            sym = str(tx["symbol"]).strip().upper()
            side = str(tx["side"]).strip().upper()
            tx_dt = _to_date(tx["trade_date"])
            raw_qty = float(tx.get("quantity", 0.0))
            raw_price = float(tx.get("price", 0.0))
            raw_amt = float(tx.get("amount") or (raw_qty * raw_price))
            fees = float(tx.get("fees") or 0.0)
            splits_s = splits_map.get(sym)

            remarks = str((tx.get("extra_data") or {}).get("Remarks", "")).strip().lower()
            is_split_row = side == "SPLIT" or (raw_price == 0 and "split" in remarks)
            is_bonus_row = side == "BONUS" or (raw_price == 0 and "bonus" in remarks)

            if adjust_for_splits and is_split_row:
                # Avoid double-counting if Yahoo auto_adjust + splits already accounts for splits
                continue
            if adjust_for_splits and is_bonus_row and _has_matching_yf_split(splits_s, tx_dt):
                # Many 1:1 bonuses appear as 2.0 in Yahoo Finance splits
                continue

            factor = _future_split_factor(splits_s, tx_dt) if adjust_for_splits else 1.0
            adj_qty = raw_qty * factor
            adj_price = (raw_price / factor) if factor > 0 else raw_price
            norm_txs.append(
                {
                    "date": tx_dt,
                    "symbol": sym,
                    "side": side,
                    "adj_qty": adj_qty,
                    "adj_price": adj_price,
                    "amount": raw_amt,
                    "fees": fees,
                }
            )

        norm_txs.sort(key=lambda x: x["date"])

        price_df = fetch_historical_prices(included_symbols, start_date=start_iso, end_date=end_iso, db_path=db_path)
        bench_series = fetch_benchmark_series(benchmark_key=benchmark_key, start_date=start_iso, end_date=end_iso)

        return _simulate_transactions_vs_benchmark(
            norm_txs=norm_txs,
            price_df=price_df,
            bench_series=bench_series,
            owner_label=owner_label,
            benchmark_key=benchmark_key,
            benchmark_label=benchmark_label,
            start_iso=start_iso,
            end_iso=end_iso,
            included_symbols=included_symbols,
            excluded_list=excluded_list,
            warnings=warnings,
            baseline_qty_map=baseline_qty_map if all_baselines else None,
            baseline_date=max_baseline_date if all_baselines else None,
            splits_map=splits_map if all_baselines else None,
        )

    # Fallback: current holdings backtest
    mode = "holdings_fallback"
    holdings = [
        h for h in all_holdings
        if _matches_segment(
            str(h.get("symbol", "")).strip().upper(),
            h.get("region"),
            h.get("advisory_source"),
        )
    ]
    if not holdings:
        start_iso = pd.Timestamp(start_date).date().isoformat() if start_date else (
            pd.Timestamp(end_iso) - pd.Timedelta(days=365)
        ).date().isoformat()
        warnings.append("No equity transactions or holdings found for the selected filter.")
        return _empty_benchmark_response(
            owner_label, mode, benchmark_key, benchmark_label, start_iso, end_iso,
            excluded_list, [], warnings,
        )

    if not region_filter and not adv_filter:
        warnings.append("No transaction history found; backtesting current holdings snapshot over the selected window.")
    start_iso = pd.Timestamp(start_date).date().isoformat() if start_date else (
        pd.Timestamp(end_iso) - pd.Timedelta(days=365)
    ).date().isoformat()
    if start_iso > end_iso:
        warnings.append(f"start_date ({start_iso}) is after end_date ({end_iso}).")
        return _empty_benchmark_response(
            owner_label, mode, benchmark_key, benchmark_label, start_iso, end_iso,
            excluded_list, [], warnings,
        )

    qty_map: dict[str, float] = {}
    fallback_price_map: dict[str, float] = {}
    for h in holdings:
        sym = str(h["symbol"]).strip().upper()
        qty_map[sym] = qty_map.get(sym, 0.0) + float(h.get("quantity", 0.0))
        if float(h.get("avg_price", 0.0)) > 0:
            fallback_price_map[sym] = float(h["avg_price"])

    included_symbols = sorted(qty_map.keys())
    price_df = fetch_historical_prices(included_symbols, start_date=start_iso, end_date=end_iso, db_path=db_path)
    bench_series = fetch_benchmark_series(benchmark_key=benchmark_key, start_date=start_iso, end_date=end_iso)

    return _simulate_holdings_vs_benchmark(
        qty_map=qty_map,
        fallback_price_map=fallback_price_map,
        price_df=price_df,
        bench_series=bench_series,
        owner_label=owner_label,
        benchmark_key=benchmark_key,
        benchmark_label=benchmark_label,
        start_iso=start_iso,
        end_iso=end_iso,
        included_symbols=included_symbols,
        excluded_list=excluded_list,
        warnings=warnings,
    )


def _build_trading_calendar(
    price_df: pd.DataFrame,
    bench_series: pd.Series,
    start_iso: str,
    end_iso: str,
) -> pd.DatetimeIndex:
    start_ts = pd.Timestamp(start_iso)
    end_ts = pd.Timestamp(end_iso)
    dates: set[pd.Timestamp] = set()
    if bench_series is not None and not bench_series.empty:
        dates.update(bench_series.index)
    if price_df is not None and not price_df.empty:
        dates.update(price_df.index)
    if not dates:
        return pd.DatetimeIndex([])
    cal = pd.DatetimeIndex(sorted(dates))
    return cal[(cal >= start_ts) & (cal <= end_ts)]


def _prepare_aligned_prices(
    price_df: pd.DataFrame,
    bench_series: pd.Series,
    calendar: pd.DatetimeIndex,
    symbols: list[str],
    warnings: list[str],
    benchmark_key: str,
) -> tuple[pd.DataFrame, pd.Series | None]:
    aligned_cols: dict[str, pd.Series] = {}
    for sym in symbols:
        if price_df is not None and not price_df.empty and sym in price_df.columns:
            s = price_df[sym].reindex(calendar).ffill()
            if s.dropna().empty:
                warnings.append(f"No historical prices found for {sym} in window.")
            aligned_cols[sym] = s
        else:
            aligned_cols[sym] = pd.Series(float("nan"), index=calendar)
            warnings.append(f"No historical prices found for {sym} in window.")
    aligned_prices = pd.DataFrame(aligned_cols, index=calendar)

    aligned_bench: pd.Series | None = None
    if bench_series is not None and not bench_series.empty:
        b = bench_series.reindex(calendar).ffill().bfill()
        if not b.dropna().empty:
            aligned_bench = b
    if aligned_bench is None:
        warnings.append(f"Benchmark series unavailable for {benchmark_key}.")
    return aligned_prices, aligned_bench


def _simulate_transactions_vs_benchmark(
    norm_txs: list[dict[str, Any]],
    price_df: pd.DataFrame,
    bench_series: pd.Series,
    owner_label: str,
    benchmark_key: str,
    benchmark_label: str,
    start_iso: str,
    end_iso: str,
    included_symbols: list[str],
    excluded_list: list[str],
    warnings: list[str],
    baseline_qty_map: dict[str, float] | None = None,
    baseline_date: date | None = None,
    splits_map: dict[str, pd.Series] | None = None,
) -> dict[str, Any]:
    calendar = _build_trading_calendar(price_df, bench_series, start_iso, end_iso)
    if calendar.empty:
        warnings.append("No market trading dates available in the selected window.")
        return _empty_benchmark_response(
            owner_label, "transactions", benchmark_key, benchmark_label, start_iso, end_iso,
            excluded_list, included_symbols, warnings,
        )

    aligned_prices, aligned_bench = _prepare_aligned_prices(
        price_df, bench_series, calendar, included_symbols, warnings, benchmark_key
    )

    first_day = calendar[0].date()
    qty_map: dict[str, float] = {s: 0.0 for s in included_symbols}
    last_known_price: dict[str, float] = {}

    def apply_tx(tx: dict[str, Any]) -> float:
        """Apply a transaction to qty_map and return net external capital inflow (+buy / -sell / -div)."""
        sym = tx["symbol"]
        side = tx["side"]
        adj_qty = tx["adj_qty"]
        amt = tx["amount"]
        fees = tx["fees"]
        if tx["adj_price"] > 0:
            last_known_price[sym] = tx["adj_price"]

        if side == "BUY":
            qty_map[sym] = qty_map.get(sym, 0.0) + adj_qty
            return amt + fees
        if side == "SELL":
            held = qty_map.get(sym, 0.0)
            qty_map[sym] = max(0.0, held - adj_qty)
            return -(max(amt - fees, 0.0))
        if side == "DIVIDEND":
            return -amt
        if side in ("BONUS", "SPLIT"):
            qty_map[sym] = qty_map.get(sym, 0.0) + adj_qty
            return 0.0
        return 0.0

    tx_idx = 0
    if baseline_qty_map is not None and baseline_date is not None:
        # Initialize from depository baseline (split-adjusted to baseline_date)
        for sym, bq in baseline_qty_map.items():
            factor = _future_split_factor((splits_map or {}).get(sym), baseline_date)
            qty_map[sym] = float(bq) * factor

        if first_day <= baseline_date:
            # Unwind trades in [first_day, baseline_date] so qty_map reflects morning of first_day
            for tx in norm_txs:
                if tx["date"] < first_day:
                    if tx["adj_price"] > 0:
                        last_known_price[tx["symbol"]] = tx["adj_price"]
                elif tx["date"] <= baseline_date:
                    sym = tx["symbol"]
                    if tx["adj_price"] > 0:
                        last_known_price[sym] = tx["adj_price"]
                    if tx["side"] in ("BUY", "BONUS", "SPLIT"):
                        qty_map[sym] = qty_map.get(sym, 0.0) - tx["adj_qty"]
                    elif tx["side"] == "SELL":
                        qty_map[sym] = qty_map.get(sym, 0.0) + tx["adj_qty"]
            for sym in list(qty_map.keys()):
                if qty_map[sym] < 1e-6:
                    qty_map[sym] = 0.0
            while tx_idx < len(norm_txs) and norm_txs[tx_idx]["date"] < first_day:
                tx_idx += 1
        else:
            # Window begins after baseline_date: replay intermediate trades forward
            while tx_idx < len(norm_txs) and norm_txs[tx_idx]["date"] < first_day:
                if norm_txs[tx_idx]["date"] > baseline_date:
                    apply_tx(norm_txs[tx_idx])
                tx_idx += 1
    else:
        # Replay trades strictly prior to the first trading day of the window
        while tx_idx < len(norm_txs) and norm_txs[tx_idx]["date"] < first_day:
            apply_tx(norm_txs[tx_idx])
            tx_idx += 1

    def mark_portfolio(ts: pd.Timestamp) -> float:
        total = 0.0
        for sym, q in qty_map.items():
            if q <= 1e-9:
                continue
            px = aligned_prices.at[ts, sym]
            if pd.notna(px) and float(px) > 0:
                last_known_price[sym] = float(px)
                total += q * float(px)
            elif sym in last_known_price:
                total += q * last_known_price[sym]
        return total

    opening_value = mark_portfolio(calendar[0])
    net_invested = opening_value
    total_capital_in = opening_value
    total_capital_out = 0.0

    bench_units = 0.0
    bench_base_price: float | None = None
    if aligned_bench is not None and opening_value > 0:
        b0 = float(aligned_bench.iloc[0])
        if b0 > 0:
            bench_units = opening_value / b0
            bench_base_price = b0

    xirr_port_cfs: list[tuple[date, float]] = []
    xirr_bench_cfs: list[tuple[date, float]] = []
    if opening_value > 0:
        xirr_port_cfs.append((first_day, -opening_value))
        xirr_bench_cfs.append((first_day, -opening_value))

    timeseries: list[dict[str, Any]] = []
    twr_index = 100.0
    prev_value = opening_value
    portfolio_active = opening_value > 0

    for i, ts in enumerate(calendar):
        day_date = ts.date()
        day_ext_flow = 0.0
        while tx_idx < len(norm_txs) and norm_txs[tx_idx]["date"] <= day_date:
            tx = norm_txs[tx_idx]
            flow = apply_tx(tx)
            day_ext_flow += flow
            if flow > 0:
                total_capital_in += flow
                xirr_port_cfs.append((day_date, -flow))
                xirr_bench_cfs.append((day_date, -flow))
            elif flow < 0:
                total_capital_out += -flow
                xirr_port_cfs.append((day_date, -flow))
                xirr_bench_cfs.append((day_date, -flow))
            tx_idx += 1

        net_invested += day_ext_flow
        port_val = mark_portfolio(ts)

        bench_px: float | None = None
        bench_val: float | None = None
        if aligned_bench is not None:
            raw_b = aligned_bench.loc[ts]
            if pd.notna(raw_b) and float(raw_b) > 0:
                bench_px = float(raw_b)
                if day_ext_flow != 0.0:
                    bench_units += day_ext_flow / bench_px
                bench_val = bench_units * bench_px

        # Update TWR
        if not portfolio_active:
            if day_ext_flow > 0 or port_val > 0:
                portfolio_active = True
                if day_ext_flow > 0:
                    daily_ret = (port_val / day_ext_flow) - 1.0
                    twr_index = 100.0 * (1.0 + daily_ret)
                if bench_px is not None and bench_base_price is None:
                    bench_base_price = bench_px
        else:
            if i == 0:
                # Opening portfolio entered on day 0
                denom = prev_value + day_ext_flow
                if denom > 0 and prev_value == 0:
                    twr_index = 100.0 * (port_val / denom)
                elif prev_value > 0:
                    daily_ret = (port_val - day_ext_flow) / prev_value - 1.0
                    twr_index *= 1.0 + daily_ret
            else:
                if prev_value > 0:
                    daily_ret = (port_val - day_ext_flow) / prev_value - 1.0
                    twr_index *= 1.0 + daily_ret
                elif day_ext_flow > 0:
                    daily_ret = (port_val / day_ext_flow) - 1.0
                    twr_index *= 1.0 + daily_ret

        prev_value = port_val

        bench_idx: float | None = None
        bench_ret_pct: float | None = None
        if bench_px is not None and bench_base_price and bench_base_price > 0:
            bench_idx = (bench_px / bench_base_price) * 100.0
            bench_ret_pct = bench_idx - 100.0

        timeseries.append(
            {
                "date": day_date.isoformat(),
                "portfolio_value": round(port_val, 2),
                "net_invested": round(net_invested, 2),
                "benchmark_value": round(bench_val, 2) if bench_val is not None else None,
                "portfolio_index": round(twr_index, 4),
                "benchmark_index": round(bench_idx, 4) if bench_idx is not None else None,
                "portfolio_return_pct": round(twr_index - 100.0, 2),
                "benchmark_return_pct": round(bench_ret_pct, 2) if bench_ret_pct is not None else None,
            }
        )

    end_day = calendar[-1].date()
    end_port_val = timeseries[-1]["portfolio_value"] if timeseries else 0.0
    end_bench_val = timeseries[-1]["benchmark_value"] if timeseries else None

    if end_port_val > 0 or total_capital_out > 0:
        xirr_port_cfs.append((end_day, end_port_val))
    if end_bench_val is not None and (end_bench_val > 0 or total_capital_out > 0):
        xirr_bench_cfs.append((end_day, end_bench_val))

    port_xirr = compute_xirr(xirr_port_cfs)
    bench_xirr = compute_xirr(xirr_bench_cfs) if end_bench_val is not None else None

    port_simple = (
        ((end_port_val + total_capital_out - total_capital_in) / total_capital_in * 100.0)
        if total_capital_in > 0
        else None
    )
    port_twr = (timeseries[-1]["portfolio_return_pct"] if portfolio_active and timeseries else None)
    bench_ret = timeseries[-1]["benchmark_return_pct"] if timeseries else None
    alpha_twr = (round(port_twr - bench_ret, 2) if port_twr is not None and bench_ret is not None else None)
    alpha_xirr = (
        round((port_xirr - bench_xirr) * 100.0, 2)
        if port_xirr is not None and bench_xirr is not None
        else None
    )

    return {
        "summary": {
            "owner": owner_label,
            "mode": "transactions",
            "benchmark_key": benchmark_key,
            "benchmark_label": benchmark_label,
            "start_date": start_iso,
            "end_date": end_iso,
            "portfolio_start_value": round(opening_value, 2),
            "portfolio_end_value": round(end_port_val, 2),
            "net_invested": round(net_invested, 2),
            "portfolio_simple_return_pct": round(port_simple, 2) if port_simple is not None else None,
            "portfolio_twr_pct": round(port_twr, 2) if port_twr is not None else None,
            "portfolio_xirr": round(port_xirr, 6) if port_xirr is not None else None,
            "benchmark_matched_value": round(end_bench_val, 2) if end_bench_val is not None else None,
            "benchmark_simple_return_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_twr_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_xirr": round(bench_xirr, 6) if bench_xirr is not None else None,
            "alpha_twr_pct": alpha_twr,
            "alpha_xirr_pct": alpha_xirr,
        },
        "timeseries": timeseries,
        "included_symbols": included_symbols,
        "excluded_symbols": excluded_list,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _simulate_holdings_vs_benchmark(
    qty_map: dict[str, float],
    fallback_price_map: dict[str, float],
    price_df: pd.DataFrame,
    bench_series: pd.Series,
    owner_label: str,
    benchmark_key: str,
    benchmark_label: str,
    start_iso: str,
    end_iso: str,
    included_symbols: list[str],
    excluded_list: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    calendar = _build_trading_calendar(price_df, bench_series, start_iso, end_iso)
    if calendar.empty:
        warnings.append("No market trading dates available in the selected window.")
        return _empty_benchmark_response(
            owner_label, "holdings_fallback", benchmark_key, benchmark_label, start_iso, end_iso,
            excluded_list, included_symbols, warnings,
        )

    aligned_prices, aligned_bench = _prepare_aligned_prices(
        price_df, bench_series, calendar, included_symbols, warnings, benchmark_key
    )
    # Small backfill at window start for holiday mismatches
    aligned_prices = aligned_prices.bfill(limit=5)

    def val_on(ts: pd.Timestamp) -> float:
        total = 0.0
        for sym, qty in qty_map.items():
            px = aligned_prices.at[ts, sym]
            if pd.notna(px) and float(px) > 0:
                total += qty * float(px)
            elif sym in fallback_price_map:
                total += qty * fallback_price_map[sym]
        return total

    v0 = val_on(calendar[0])
    b0 = float(aligned_bench.iloc[0]) if aligned_bench is not None and float(aligned_bench.iloc[0]) > 0 else None
    bench_units = (v0 / b0) if (b0 and v0 > 0) else 0.0

    timeseries: list[dict[str, Any]] = []
    for ts in calendar:
        pv = val_on(ts)
        p_idx = (pv / v0 * 100.0) if v0 > 0 else 100.0
        p_ret = p_idx - 100.0

        b_val: float | None = None
        b_idx: float | None = None
        b_ret: float | None = None
        if aligned_bench is not None and b0:
            bx = float(aligned_bench.loc[ts])
            if pd.notna(bx) and bx > 0:
                b_val = bench_units * bx
                b_idx = bx / b0 * 100.0
                b_ret = b_idx - 100.0

        timeseries.append(
            {
                "date": ts.date().isoformat(),
                "portfolio_value": round(pv, 2),
                "net_invested": round(v0, 2),
                "benchmark_value": round(b_val, 2) if b_val is not None else None,
                "portfolio_index": round(p_idx, 4),
                "benchmark_index": round(b_idx, 4) if b_idx is not None else None,
                "portfolio_return_pct": round(p_ret, 2),
                "benchmark_return_pct": round(b_ret, 2) if b_ret is not None else None,
            }
        )

    v_end = timeseries[-1]["portfolio_value"]
    b_end = timeseries[-1]["benchmark_value"]
    d0 = calendar[0].date()
    dn = calendar[-1].date()

    port_xirr = compute_xirr([(d0, -v0), (dn, v_end)]) if v0 > 0 else None
    bench_xirr = compute_xirr([(d0, -v0), (dn, b_end)]) if (v0 > 0 and b_end is not None) else None
    port_ret = timeseries[-1]["portfolio_return_pct"]
    bench_ret = timeseries[-1]["benchmark_return_pct"]
    alpha_twr = round(port_ret - bench_ret, 2) if bench_ret is not None else None
    alpha_xirr = (
        round((port_xirr - bench_xirr) * 100.0, 2)
        if port_xirr is not None and bench_xirr is not None
        else None
    )

    return {
        "summary": {
            "owner": owner_label,
            "mode": "holdings_fallback",
            "benchmark_key": benchmark_key,
            "benchmark_label": benchmark_label,
            "start_date": start_iso,
            "end_date": end_iso,
            "portfolio_start_value": round(v0, 2),
            "portfolio_end_value": round(v_end, 2),
            "net_invested": round(v0, 2),
            "portfolio_simple_return_pct": round(port_ret, 2),
            "portfolio_twr_pct": round(port_ret, 2),
            "portfolio_xirr": round(port_xirr, 6) if port_xirr is not None else None,
            "benchmark_matched_value": round(b_end, 2) if b_end is not None else None,
            "benchmark_simple_return_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_twr_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_xirr": round(bench_xirr, 6) if bench_xirr is not None else None,
            "alpha_twr_pct": alpha_twr,
            "alpha_xirr_pct": alpha_xirr,
        },
        "timeseries": timeseries,
        "included_symbols": included_symbols,
        "excluded_symbols": excluded_list,
        "warnings": list(dict.fromkeys(warnings)),
    }


def compare_mf_vs_benchmark(
    owner: str | None = None,
    benchmark_key: str = "NIFTY_MIDCAP_150",
    start_date: str | None = None,
    end_date: str | None = None,
    scheme_filter: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Compare mutual fund holdings (all combined or a single scheme) vs a benchmark index."""
    from concurrent.futures import ThreadPoolExecutor

    owner_label = (
        owner.strip().title()
        if owner and owner.strip().lower() not in ("all", "combined", "both", "*")
        else "Combined"
    )
    benchmark_label = BENCHMARKS.get(benchmark_key, {}).get("label", benchmark_key)
    end_iso = pd.Timestamp(end_date).date().isoformat() if end_date else date.today().isoformat()
    start_iso = (
        pd.Timestamp(start_date).date().isoformat()
        if start_date
        else (pd.Timestamp(end_iso) - pd.Timedelta(days=365)).date().isoformat()
    )
    warnings: list[str] = []

    mf_rows = query_mf_holdings(owner=owner, db_path=db_path)
    target_filter = (scheme_filter or "").strip()
    if target_filter and target_filter.upper() != "ALL":
        mf_rows = [
            m for m in mf_rows
            if str(m.get("amfi_code", "")).strip() == target_filter
            or str(m.get("scheme_name", "")).strip().lower() == target_filter.lower()
        ]

    if not mf_rows:
        warnings.append("No mutual fund holdings found for the selected filter.")
        return _empty_benchmark_response(
            owner_label, "mf_holdings", benchmark_key, benchmark_label, start_iso, end_iso,
            [], [], warnings,
        )

    # Aggregate units by scheme key (amfi_code or scheme_name), skipping non-AMFI snapshot funds
    units_map: dict[str, float] = {}
    meta_map: dict[str, tuple[str, str]] = {}
    for m in mf_rows:
        code = str(m.get("amfi_code", "")).strip()
        sname = str(m.get("scheme_name", "")).strip()
        extra = m.get("extra_data") if isinstance(m.get("extra_data"), dict) else {}
        if extra.get("manual_ltp") is not None and (not code or not code.isdigit()):
            warnings.append(f"Daily historical NAV curve unavailable for non-AMFI fund '{sname}'.")
            continue
        key = code or sname
        units_map[key] = units_map.get(key, 0.0) + float(m.get("units", 0.0))
        meta_map[key] = (code, sname)

    keys = sorted(units_map.keys())
    if not keys:
        return _empty_benchmark_response(
            owner_label, "mf_holdings", benchmark_key, benchmark_label, start_iso, end_iso,
            [], [], warnings,
        )
    nav_series_map: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(keys))) as pool:
        futs = {
            k: pool.submit(fetch_mf_historical_nav, meta_map[k][0], meta_map[k][1], start_iso, end_iso)
            for k in keys
        }
        for k, fut in futs.items():
            try:
                s = fut.result()
                if s is not None and not s.empty:
                    nav_series_map[k] = s
                else:
                    warnings.append(f"Historical NAV unavailable for '{meta_map[k][1]}'.")
            except Exception:
                warnings.append(f"Historical NAV unavailable for '{meta_map[k][1]}'.")

    if not nav_series_map:
        return _empty_benchmark_response(
            owner_label, "mf_holdings", benchmark_key, benchmark_label, start_iso, end_iso,
            [], [meta_map[k][1] for k in keys], warnings,
        )

    # Adjust effective start if selected MF(s) launched after requested start_iso
    if len(nav_series_map) == 1:
        only_series = next(iter(nav_series_map.values()))
        earliest_nav = only_series.index.min().date().isoformat()
        if earliest_nav > start_iso:
            warnings.append(f"NAV history starts on {earliest_nav}; comparison adjusted to {earliest_nav}.")
            start_iso = earliest_nav
    else:
        # For combined MFs, begin once the largest-unit fund has NAV data
        largest_k = max(nav_series_map.keys(), key=lambda k: units_map[k] * float(nav_series_map[k].iloc[-1]))
        earliest_major = nav_series_map[largest_k].index.min().date().isoformat()
        if earliest_major > start_iso:
            warnings.append(f"Primary MF NAV history starts on {earliest_major}; comparison adjusted to {earliest_major}.")
            start_iso = earliest_major

    nav_df = pd.DataFrame(nav_series_map)
    bench_series = fetch_benchmark_series(benchmark_key=benchmark_key, start_date=start_iso, end_date=end_iso)

    included_labels = [meta_map[k][1] or k for k in keys if k in nav_series_map]
    # Value each fund at its available NAV (or earliest NAV if a smaller fund launched mid-window)
    calendar = _build_trading_calendar(nav_df, bench_series, start_iso, end_iso)
    if calendar.empty:
        warnings.append("No overlapping NAV dates available in the selected window.")
        return _empty_benchmark_response(
            owner_label, "mf_holdings", benchmark_key, benchmark_label, start_iso, end_iso,
            [], included_labels, warnings,
        )

    aligned_navs = nav_df.reindex(calendar).ffill().bfill()
    aligned_bench = (
        bench_series.reindex(calendar).ffill().bfill()
        if bench_series is not None and not bench_series.empty
        else None
    )

    def val_on(ts: pd.Timestamp) -> float:
        total = 0.0
        for k, s_nav in aligned_navs.items():
            px = s_nav.loc[ts]
            if pd.notna(px) and float(px) > 0:
                total += units_map[k] * float(px)
        return total

    v0 = val_on(calendar[0])
    b0 = float(aligned_bench.iloc[0]) if (aligned_bench is not None and not aligned_bench.dropna().empty and float(aligned_bench.iloc[0]) > 0) else None
    bench_units = (v0 / b0) if (b0 and v0 > 0) else 0.0

    timeseries: list[dict[str, Any]] = []
    for ts in calendar:
        pv = val_on(ts)
        p_idx = (pv / v0 * 100.0) if v0 > 0 else 100.0
        p_ret = p_idx - 100.0

        b_val: float | None = None
        b_idx: float | None = None
        b_ret: float | None = None
        if aligned_bench is not None and b0:
            bx = float(aligned_bench.loc[ts])
            if pd.notna(bx) and bx > 0:
                b_val = bench_units * bx
                b_idx = bx / b0 * 100.0
                b_ret = b_idx - 100.0

        timeseries.append(
            {
                "date": ts.date().isoformat(),
                "portfolio_value": round(pv, 2),
                "net_invested": round(v0, 2),
                "benchmark_value": round(b_val, 2) if b_val is not None else None,
                "portfolio_index": round(p_idx, 4),
                "benchmark_index": round(b_idx, 4) if b_idx is not None else None,
                "portfolio_return_pct": round(p_ret, 2),
                "benchmark_return_pct": round(b_ret, 2) if b_ret is not None else None,
            }
        )

    v_end = timeseries[-1]["portfolio_value"]
    b_end = timeseries[-1]["benchmark_value"]
    d0, dn = calendar[0].date(), calendar[-1].date()
    port_xirr = compute_xirr([(d0, -v0), (dn, v_end)]) if v0 > 0 else None
    bench_xirr = compute_xirr([(d0, -v0), (dn, b_end)]) if (v0 > 0 and b_end is not None) else None
    port_ret = timeseries[-1]["portfolio_return_pct"]
    bench_ret = timeseries[-1]["benchmark_return_pct"]
    alpha_twr = round(port_ret - bench_ret, 2) if bench_ret is not None else None
    alpha_xirr = (
        round((port_xirr - bench_xirr) * 100.0, 2)
        if port_xirr is not None and bench_xirr is not None
        else None
    )

    return {
        "summary": {
            "owner": owner_label,
            "mode": "mf_holdings",
            "benchmark_key": benchmark_key,
            "benchmark_label": benchmark_label,
            "start_date": start_iso,
            "end_date": end_iso,
            "portfolio_start_value": round(v0, 2),
            "portfolio_end_value": round(v_end, 2),
            "net_invested": round(v0, 2),
            "portfolio_simple_return_pct": round(port_ret, 2),
            "portfolio_twr_pct": round(port_ret, 2),
            "portfolio_xirr": round(port_xirr, 6) if port_xirr is not None else None,
            "benchmark_matched_value": round(b_end, 2) if b_end is not None else None,
            "benchmark_simple_return_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_twr_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "benchmark_xirr": round(bench_xirr, 6) if bench_xirr is not None else None,
            "alpha_twr_pct": alpha_twr,
            "alpha_xirr_pct": alpha_xirr,
        },
        "timeseries": timeseries,
        "included_symbols": included_labels,
        "excluded_symbols": [],
        "warnings": list(dict.fromkeys(warnings)),
    }


def compute_cap_distribution(
    holdings: list[dict[str, Any]],
    cap_map: dict[str, dict[str, Any]],
    segment: str = "All Combined",
) -> dict[str, Any]:
    """Compute value-weighted Large/Mid/Small/Other cap distribution for Indian holdings."""
    indian_holdings = [
        h for h in holdings
        if str(h.get("region") or "India").strip().lower() != "foreign"
    ]
    seg = (segment or "All Combined").strip()
    if seg == "Equities Only":
        filtered = [h for h in indian_holdings if h.get("asset_class") == "EQUITY"]
    elif seg == "Mutual Funds Only":
        filtered = [h for h in indian_holdings if h.get("asset_class") == "MF"]
    elif seg in ("All Combined", "All", ""):
        filtered = list(indian_holdings)
    else:
        seg_l = seg.lower()
        filtered = [
            h for h in indian_holdings
            if str(h.get("advisory_source") or "Self").strip().lower() == seg_l
        ]

    large_val = mid_val = small_val = other_val = unmapped_val = 0.0
    mapped_count = 0
    latest_ts = ""

    for h in filtered:
        val = max(0.0, float(h.get("current_value") or 0.0))
        is_eq = h.get("asset_class") == "EQUITY"
        key = str(h.get("symbol") if is_eq else h.get("scheme_name") or "").strip().upper()
        cap = cap_map.get(key)
        if not cap:
            unmapped_val += val
            continue
        mapped_count += 1
        ts = str(cap.get("updated_at") or "")
        if ts > latest_ts:
            latest_ts = ts
        large_val += val * float(cap.get("large_pct") or 0.0) / 100.0
        mid_val += val * float(cap.get("mid_pct") or 0.0) / 100.0
        small_val += val * float(cap.get("small_pct") or 0.0) / 100.0
        other_val += val * float(cap.get("other_pct") or 0.0) / 100.0

    mapped_total = large_val + mid_val + small_val + other_val
    total_val = mapped_total + unmapped_val
    denom = mapped_total if mapped_total > 0 else 1.0

    return {
        "segment": seg,
        "total_value": round(total_val, 2),
        "mapped_value": round(mapped_total, 2),
        "unmapped_value": round(unmapped_val, 2),
        "large_val": round(large_val, 2),
        "mid_val": round(mid_val, 2),
        "small_val": round(small_val, 2),
        "other_val": round(other_val, 2),
        "large_pct": round(large_val / denom * 100.0, 2) if mapped_total > 0 else 0.0,
        "mid_pct": round(mid_val / denom * 100.0, 2) if mapped_total > 0 else 0.0,
        "small_pct": round(small_val / denom * 100.0, 2) if mapped_total > 0 else 0.0,
        "other_pct": round(other_val / denom * 100.0, 2) if mapped_total > 0 else 0.0,
        "coverage_pct": round(mapped_total / total_val * 100.0, 1) if total_val > 0 else 0.0,
        "mapped_count": mapped_count,
        "total_count": len(filtered),
        "last_updated": latest_ts[:10] if latest_ts else "",
    }
