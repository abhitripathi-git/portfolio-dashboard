"""On-demand market data fetcher (yfinance + mftool) with lightweight in-memory TTL caching.

Public market histories and live prices are NEVER persisted to SQLite.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from config import BENCHMARKS, DB_PATH, SYMBOL_OVERRIDES
from core.db import get_symbol_aliases, save_symbol_alias

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 900  # 15 minutes


class _TTLCache:
    """Thread-safe in-memory TTL cache."""

    def __init__(self, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: Any) -> tuple[bool, Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return False, None
            return True, value

    def set(self, key: Any, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._store[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_CACHE = _TTLCache(ttl_seconds=DEFAULT_CACHE_TTL_SECONDS)
_MFTOOL_LOCK = threading.RLock()
_MFTOOL_INSTANCE: Any = None


def clear_market_cache() -> None:
    """Clear all in-memory market data caches."""
    _CACHE.clear()


def _get_mftool() -> Any:
    """Return a cached mftool.Mftool instance, or None if initialization fails."""
    global _MFTOOL_INSTANCE
    with _MFTOOL_LOCK:
        if _MFTOOL_INSTANCE is None:
            try:
                from mftool import Mftool

                _MFTOOL_INSTANCE = Mftool()
            except Exception as exc:
                logger.warning("Failed to initialize Mftool: %s", exc)
                return None
        return _MFTOOL_INSTANCE


def _db_exists(db_path: Path | str | None) -> bool:
    path = Path(db_path) if db_path else DB_PATH
    return path.exists()


def _normalize_dt_index(obj: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """Ensure index is a sorted, tz-naive DatetimeIndex normalized to midnight."""
    if obj.empty:
        return obj
    idx = pd.to_datetime(obj.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    idx = idx.normalize()
    out = obj.copy()
    out.index = idx
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def _infer_currency(yahoo_symbol: str) -> str:
    upper = yahoo_symbol.upper()
    if upper.endswith((".NS", ".BO")) or upper.startswith(("^NSE", "^BSESN", "^CRSLDX", "^NSMIDCP")):
        return "INR"
    return "USD"


def fetch_usdinr_rate() -> float | None:
    """Fetch latest USD/INR exchange rate via Yahoo Finance (`USDINR=X`), cached in memory."""
    cache_key = ("fx", "USDINR=X")
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached
    try:
        hist = yf.Ticker("USDINR=X").history(period="5d", auto_adjust=True)
        if hist is not None and not hist.empty and "Close" in hist.columns:
            valid = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            if not valid.empty:
                rate = round(float(valid.iloc[-1]), 4)
                _CACHE.set(cache_key, rate)
                return rate
    except Exception as exc:
        logger.debug("Failed to fetch USDINR=X: %s", exc)
    _CACHE.set(cache_key, None, ttl_seconds=120)
    return None


def _extract_close_series_from_download(
    data: pd.DataFrame | None,
    yahoo_symbol: str,
    requested_count: int = 1,
) -> pd.Series:
    """Extract a clean Close price Series for one ticker from yf.download output."""
    if data is None or data.empty:
        return pd.Series(dtype=float)

    series: pd.Series | None = None
    cols = data.columns

    if isinstance(cols, pd.MultiIndex):
        level0 = list(cols.get_level_values(0))
        level1 = list(cols.get_level_values(1))
        if "Close" in level0:
            sub = data["Close"]
            if isinstance(sub, pd.Series):
                series = sub
            elif yahoo_symbol in sub.columns:
                series = sub[yahoo_symbol]
            elif requested_count == 1 and sub.shape[1] == 1:
                series = sub.iloc[:, 0]
        elif yahoo_symbol in level0:
            sub = data[yahoo_symbol]
            if isinstance(sub, pd.DataFrame) and "Close" in sub.columns:
                series = sub["Close"]
        elif "Close" in level1 and yahoo_symbol in level0:
            series = data[(yahoo_symbol, "Close")]
    else:
        if "Close" in cols:
            col_data = data["Close"]
            series = col_data if isinstance(col_data, pd.Series) else col_data.iloc[:, 0]
        elif yahoo_symbol in cols:
            col_data = data[yahoo_symbol]
            series = col_data if isinstance(col_data, pd.Series) else col_data.iloc[:, 0]

    if series is None:
        return pd.Series(dtype=float)

    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return pd.Series(dtype=float)
    return _normalize_dt_index(clean)


_ABBREV_MAP = {
    "FIN": "FINANCE",
    "TECH": "TECHNOLOGIES",
    "TECHNOLOGI": "TECHNOLOGIES",
    "INDL": "INDUSTRIAL",
    "CORPN": "CORPORATION",
    "MGT": "MANAGEMENT",
    "INTL": "INTERNATIONAL",
    "HLDG": "HOLDINGS",
    "INV": "INVESTMENT",
    "CHEM": "CHEMICALS",
    "AUTO": "AUTOMOTIVE",
    "IND": "INDUSTRIES",
    "HEALTH": "HEALTHCARE",
    "GEM": "GEMOLOGICAL",
    "PHARMA": "PHARMACEUTICALS",
    "SPECIAL": "SPECIALITY",
}


def _expand_company_query(raw: str) -> str:
    """Normalize a truncated broker scrip/company name for NSE & Yahoo search."""
    cleaned = re.sub(r"[()]", " ", raw.upper())
    cleaned = cleaned.replace("&", " AND ")
    tokens = []
    for tok in re.split(r"[\s,]+", cleaned):
        t = tok.strip(".")
        if not t:
            continue
        tokens.append(_ABBREV_MAP.get(t, t))
    return " ".join(tokens)


def _fetch_nse_equity_directory() -> list[dict[str, str]]:
    """Fetch and cache the official NSE EQUITY_L.csv symbol-to-company directory."""
    cache_key = ("nse_equity_l",)
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached

    rows: list[dict[str, str]] = []
    try:
        import io
        import requests

        resp = requests.get(
            "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200 and resp.text:
            df = pd.read_csv(io.StringIO(resp.text))
            df.columns = [str(c).strip().upper() for c in df.columns]
            sym_col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
            name_col = "NAME OF COMPANY" if "NAME OF COMPANY" in df.columns else df.columns[1]
            isin_col = "ISIN NUMBER" if "ISIN NUMBER" in df.columns else None
            for _, r in df.iterrows():
                sym = str(r.get(sym_col, "")).strip().upper()
                name = str(r.get(name_col, "")).strip().upper()
                isin = str(r.get(isin_col, "")).strip().upper() if isin_col else ""
                if sym and name:
                    rows.append({"symbol": sym, "name": name, "isin": isin})
    except Exception as exc:
        logger.debug("NSE EQUITY_L fetch failed: %s", exc)

    _CACHE.set(cache_key, rows, ttl_seconds=3600 if rows else 120)
    return rows


def _verify_yahoo_candidate(candidate: str) -> bool:
    """Check whether Yahoo Finance has recent EOD pricing for candidate."""
    try:
        hist = yf.Ticker(candidate).history(period="5d", auto_adjust=True)
        if hist is not None and not hist.empty and "Close" in hist.columns:
            valid = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            return not valid.empty
    except Exception as exc:
        logger.debug("Probe failed for %s: %s", candidate, exc)
    return False


def _match_nse_directory(raw: str, clean_isin: str, directory: list[dict[str, str]]) -> str | None:
    """Match ISIN or company name against the cached NSE directory."""
    if not directory:
        return None

    if clean_isin:
        for row in directory:
            if row["isin"] == clean_isin:
                return f"{row['symbol']}.NS"

    query = _expand_company_query(raw)
    query_tokens = [t for t in query.split() if t not in {"LIMITED", "LTD", "INDIA"}]
    best_score = 0.0
    best_symbol: str | None = None

    for row in directory:
        sym = row["symbol"]
        if sym == raw:
            return f"{sym}.NS"
        name = _expand_company_query(row["name"])
        if name.startswith(query):
            return f"{sym}.NS"

        name_tokens = set(name.split())
        if query_tokens and all(
            any(nt.startswith(qt) for nt in name_tokens) for qt in query_tokens
        ):
            score = 0.9 + SequenceMatcher(None, query, name).ratio() * 0.1
        else:
            score = SequenceMatcher(None, query, name).ratio()

        if score > best_score:
            best_score = score
            best_symbol = sym

    if best_symbol and best_score >= 0.72:
        return f"{best_symbol}.NS"
    return None


def _search_yahoo_ticker(raw: str) -> str | None:
    """Fallback company-name lookup using yf.Search, preferring NSE (.NS) then BSE (.BO)."""
    queries = list(dict.fromkeys([raw, _expand_company_query(raw)]))
    for q in queries:
        if not q:
            continue
        try:
            res = yf.Search(q, max_results=6)
            quotes = getattr(res, "quotes", None) or []
            ns_cands = [str(item.get("symbol", "")).strip() for item in quotes if str(item.get("symbol", "")).endswith(".NS")]
            bo_cands = [str(item.get("symbol", "")).strip() for item in quotes if str(item.get("symbol", "")).endswith(".BO")]
            for cand in ns_cands + bo_cands:
                if _verify_yahoo_candidate(cand):
                    return cand
        except Exception as exc:
            logger.debug("yf.Search failed for %s: %s", q, exc)
    return None


def resolve_yahoo_symbol(symbol: str, isin: str = "", db_path: Path | str | None = None) -> str:
    """Resolve a broker/raw equity symbol or company name into a Yahoo Finance ticker (.NS / .BO).

    Checks `symbol_aliases` in SQLite, `SYMBOL_OVERRIDES` in config.py, exact ticker
    probes (`.NS`/`.BO`), official NSE `EQUITY_L.csv`, and `yf.Search` fallback.
    """
    raw = str(symbol or "").strip().upper()
    clean_isin = str(isin or "").strip().upper()
    if not raw:
        return ""

    cache_key = ("resolve", raw, clean_isin, str(db_path or ""))
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached

    def _remember(resolved_sym: str, persist: bool = True) -> str:
        _CACHE.set(cache_key, resolved_sym)
        if persist and _db_exists(db_path):
            try:
                save_symbol_alias(raw, resolved_sym, isin=clean_isin, db_path=db_path)
            except Exception:
                pass
        return resolved_sym

    # 1. Check DB symbol_aliases if DB exists
    if _db_exists(db_path):
        try:
            aliases = get_symbol_aliases(db_path=db_path)
            if raw in aliases and aliases[raw].get("yahoo_symbol"):
                resolved = str(aliases[raw]["yahoo_symbol"]).strip()
                _CACHE.set(cache_key, resolved)
                return resolved
            if clean_isin:
                for row in aliases.values():
                    if row.get("isin", "").strip().upper() == clean_isin and row.get("yahoo_symbol"):
                        resolved = str(row["yahoo_symbol"]).strip()
                        _CACHE.set(cache_key, resolved)
                        return resolved
        except Exception as exc:
            logger.debug("Could not read symbol_aliases: %s", exc)

    # 2. Check config SYMBOL_OVERRIDES
    if raw in SYMBOL_OVERRIDES:
        return _remember(SYMBOL_OVERRIDES[raw], persist=False)

    # 3. Already exchange-qualified (.NS/.BO) or index ticker (^NSEI)
    if raw.startswith("^") or re.search(r"\.(NS|BO|NSE|BSE)$", raw):
        _CACHE.set(cache_key, raw)
        return raw

    # 4. If it looks like a clean exchange ticker (no spaces/punctuation), probe .NS / .BO
    if re.fullmatch(r"[A-Z0-9&-]+", raw):
        for suffix in (".NS", ".BO"):
            candidate = f"{raw}{suffix}"
            if _verify_yahoo_candidate(candidate):
                return _remember(candidate)

    # 5. Match against official NSE EQUITY_L.csv directory (great for 5paisa company names)
    nse_dir = _fetch_nse_equity_directory()
    nse_match = _match_nse_directory(raw, clean_isin, nse_dir)
    if nse_match and _verify_yahoo_candidate(nse_match):
        return _remember(nse_match)

    # 6. Fallback to yf.Search (handles ETFs, BSE-only, or abbreviated names)
    searched = _search_yahoo_ticker(raw)
    if searched:
        return _remember(searched)

    # Fallback default without persisting unverified mapping
    fallback = f"{re.sub(r'[^A-Z0-9&-]+', '', raw)}.NS" if raw else ""
    _CACHE.set(cache_key, fallback, ttl_seconds=120)
    return fallback


def _price_dict_from_series(series: pd.Series, yahoo_symbol: str) -> dict[str, Any]:
    currency = _infer_currency(yahoo_symbol)
    if series.empty:
        return {
            "price": None,
            "prev_close": None,
            "change_pct": None,
            "yahoo_symbol": yahoo_symbol,
            "currency": currency,
        }
    price = float(series.iloc[-1])
    prev_close = float(series.iloc[-2]) if len(series) >= 2 else price
    change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    return {
        "price": round(price, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 4),
        "yahoo_symbol": yahoo_symbol,
        "currency": currency,
    }


def fetch_latest_prices(
    symbols: list[str],
    db_path: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch latest adjusted price and daily change for a list of equity symbols.

    Returns `{symbol: {"price", "prev_close", "change_pct", "yahoo_symbol", "currency"}}`.
    Missing or delisted tickers return `price: None` without raising.
    """
    results: dict[str, dict[str, Any]] = {}
    if not symbols:
        return results

    uncached: list[str] = []
    for sym in symbols:
        if not sym or not str(sym).strip():
            continue
        cache_key = ("latest_price", str(sym).strip().upper(), str(db_path or ""))
        hit, cached = _CACHE.get(cache_key)
        if hit:
            results[sym] = cached
        else:
            uncached.append(sym)

    if not uncached:
        return results

    sym_to_yahoo: dict[str, str] = {}
    if len(uncached) > 1:
        pool = ThreadPoolExecutor(max_workers=min(6, len(uncached)))
        timed_out = False
        try:
            futs = {sym: pool.submit(resolve_yahoo_symbol, sym, db_path) for sym in uncached}
            for sym, fut in futs.items():
                try:
                    sym_to_yahoo[sym] = fut.result(timeout=12)
                except Exception:
                    timed_out = True
                    sym_to_yahoo[sym] = f"{sym}.NS"
        finally:
            pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
    else:
        for sym in uncached:
            sym_to_yahoo[sym] = resolve_yahoo_symbol(sym, db_path=db_path)

    unique_yahoo = sorted(set(sym_to_yahoo.values()))
    batch_df: pd.DataFrame | None = None
    if unique_yahoo:
        try:
            tickers_arg = unique_yahoo[0] if len(unique_yahoo) == 1 else unique_yahoo
            batch_df = yf.download(
                tickers=tickers_arg,
                period="5d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            logger.warning("Batch yf.download failed for latest prices: %s", exc)
            batch_df = None

    for sym in uncached:
        ysym = sym_to_yahoo[sym]
        series = _extract_close_series_from_download(batch_df, ysym, requested_count=len(unique_yahoo))

        # Per-ticker fallback if batch missed it
        if series.empty:
            try:
                hist = yf.Ticker(ysym).history(period="5d", auto_adjust=True)
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    clean = pd.to_numeric(hist["Close"], errors="coerce").dropna()
                    if not clean.empty:
                        series = _normalize_dt_index(clean)
            except Exception as exc:
                logger.debug("Fallback Ticker.history failed for %s (%s): %s", sym, ysym, exc)

        payload = _price_dict_from_series(series, ysym)
        cache_key = ("latest_price", str(sym).strip().upper(), str(db_path or ""))
        ttl = DEFAULT_CACHE_TTL_SECONDS
        _CACHE.set(cache_key, payload, ttl_seconds=ttl)
        results[sym] = payload

    return results


def _inclusive_yf_end(end_date: str | None) -> tuple[str | None, str | None]:
    """Convert inclusive YYYY-MM-DD end_date into yfinance exclusive end (+1 day)."""
    if not end_date:
        return None, None
    parsed = pd.Timestamp(end_date).date()
    return parsed.isoformat(), (parsed + timedelta(days=1)).isoformat()


def fetch_historical_prices(
    symbols: list[str],
    start_date: str,
    end_date: str | None = None,
    db_path: Path | str | None = None,
) -> pd.DataFrame:
    """Fetch split/dividend/bonus-adjusted daily close prices (`auto_adjust=True`).

    Returns a DataFrame indexed by tz-naive date with the requested symbols as columns.
    """
    clean_symbols = [s for s in dict.fromkeys(symbols) if s and str(s).strip()]
    if not clean_symbols:
        return pd.DataFrame()

    start_iso = pd.Timestamp(start_date).date().isoformat()
    end_iso, yf_end_iso = _inclusive_yf_end(end_date)

    cache_key = ("hist_prices", tuple(clean_symbols), start_iso, end_iso or "", str(db_path or ""))
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached.copy()

    sym_to_yahoo = {sym: resolve_yahoo_symbol(sym, db_path=db_path) for sym in clean_symbols}
    unique_yahoo = sorted(set(sym_to_yahoo.values()))

    batch_df: pd.DataFrame | None = None
    try:
        tickers_arg = unique_yahoo[0] if len(unique_yahoo) == 1 else unique_yahoo
        batch_df = yf.download(
            tickers=tickers_arg,
            start=start_iso,
            end=yf_end_iso,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("Batch historical yf.download failed: %s", exc)

    series_map: dict[str, pd.Series] = {}
    for sym in clean_symbols:
        ysym = sym_to_yahoo[sym]
        s = _extract_close_series_from_download(batch_df, ysym, requested_count=len(unique_yahoo))
        if s.empty:
            try:
                hist = yf.Ticker(ysym).history(start=start_iso, end=yf_end_iso, auto_adjust=True)
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    clean = pd.to_numeric(hist["Close"], errors="coerce").dropna()
                    if not clean.empty:
                        s = _normalize_dt_index(clean)
            except Exception as exc:
                logger.debug("Historical fallback failed for %s (%s): %s", sym, ysym, exc)
        if not s.empty:
            if end_iso:
                s = s.loc[: pd.Timestamp(end_iso)]
            series_map[sym] = s

    if not series_map:
        return pd.DataFrame(columns=clean_symbols)

    df = pd.DataFrame(series_map)
    for sym in clean_symbols:
        if sym not in df.columns:
            df[sym] = float("nan")
    df = df[clean_symbols]
    df = _normalize_dt_index(df)
    _CACHE.set(cache_key, df.copy())
    return df


def fetch_benchmark_series(
    benchmark_key: str = "NIFTY_50",
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.Series:
    """Fetch adjusted close series for a configured benchmark (trying candidate tickers in order)."""
    start_iso = (
        pd.Timestamp(start_date).date().isoformat()
        if start_date
        else (date.today() - timedelta(days=365)).isoformat()
    )
    end_iso, yf_end_iso = _inclusive_yf_end(end_date)

    cache_key = ("benchmark", benchmark_key, start_iso, end_iso or "")
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached.copy()

    cfg = BENCHMARKS.get(benchmark_key)
    candidates = list(cfg["tickers"]) if cfg and cfg.get("tickers") else [benchmark_key]

    for ticker in candidates:
        try:
            df = yf.download(
                tickers=ticker,
                start=start_iso,
                end=yf_end_iso,
                auto_adjust=True,
                progress=False,
            )
            series = _extract_close_series_from_download(df, ticker, requested_count=1)
            if series.empty:
                hist = yf.Ticker(ticker).history(start=start_iso, end=yf_end_iso, auto_adjust=True)
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    clean = pd.to_numeric(hist["Close"], errors="coerce").dropna()
                    if not clean.empty:
                        series = _normalize_dt_index(clean)
            if not series.empty:
                if end_iso:
                    series = series.loc[: pd.Timestamp(end_iso)]
                series.name = benchmark_key
                _CACHE.set(cache_key, series.copy())
                return series
        except Exception as exc:
            logger.debug("Benchmark ticker %s failed: %s", ticker, exc)

    return pd.Series(dtype=float, name=benchmark_key)


def _normalize_scheme_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _best_scheme_match(scheme_name: str, all_schemes: dict[str, str]) -> str | None:
    """Find best matching AMFI scheme code for a scheme name."""
    target_norm = _normalize_scheme_text(scheme_name)
    if not target_norm or not all_schemes:
        return None

    target_tokens = set(target_norm.split())
    wants_direct = "direct" in target_tokens
    wants_regular = "regular" in target_tokens
    wants_idcw = bool(target_tokens & {"idcw", "dividend", "div"})

    best_code: str | None = None
    best_score = -1.0

    for code, name in all_schemes.items():
        cand_norm = _normalize_scheme_text(str(name))
        if not cand_norm:
            continue
        if cand_norm == target_norm:
            return str(code)

        cand_tokens = set(cand_norm.split())
        overlap = len(target_tokens & cand_tokens) / max(len(target_tokens), 1)
        seq_ratio = SequenceMatcher(None, target_norm, cand_norm).ratio()
        score = 0.6 * overlap + 0.4 * seq_ratio

        # Plan/option alignment bonus/penalty
        if wants_direct and "direct" in cand_tokens:
            score += 0.15
        elif wants_direct and "regular" in cand_tokens:
            score -= 0.15
        if wants_regular and "regular" in cand_tokens:
            score += 0.15
        if not wants_idcw and (cand_tokens & {"idcw", "dividend"}):
            score -= 0.15
        elif wants_idcw and (cand_tokens & {"idcw", "dividend"}):
            score += 0.10

        if score > best_score:
            best_score = score
            best_code = str(code)

    return best_code if best_score >= 0.45 else None


def fetch_mf_nav(amfi_code: str = "", scheme_name: str = "") -> dict[str, Any] | None:
    """Look up latest mutual fund NAV via mftool (AMFI) by AMFI code or scheme name."""
    code = str(amfi_code or "").strip()
    name = str(scheme_name or "").strip()
    if not code and not name:
        return None

    cache_key = ("mf_nav", code, name.lower())
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return dict(cached) if cached is not None else None

    mf = _get_mftool()
    if mf is None:
        return None

    try:
        quote: dict[str, Any] | None = None
        if code:
            raw_quote = mf.get_scheme_quote(code)
            if isinstance(raw_quote, dict) and raw_quote.get("nav"):
                quote = raw_quote

        if quote is None and name:
            all_codes = mf.get_scheme_codes(as_json=False)
            if isinstance(all_codes, dict):
                matched_code = _best_scheme_match(name, all_codes)
                if matched_code:
                    raw_quote = mf.get_scheme_quote(matched_code)
                    if isinstance(raw_quote, dict) and raw_quote.get("nav"):
                        quote = raw_quote

        if not quote or not quote.get("nav"):
            _CACHE.set(cache_key, None, ttl_seconds=120)
            return None

        nav_val = float(str(quote["nav"]).replace(",", "").strip())
        result = {
            "scheme_code": str(quote.get("scheme_code", code)).strip(),
            "scheme_name": str(quote.get("scheme_name", name)).strip(),
            "nav": nav_val,
            "last_updated": str(quote.get("last_updated", "")).strip(),
            "plan": str(quote.get("plan", "")).strip(),
            "option": str(quote.get("option", "")).strip(),
        }
        _CACHE.set(cache_key, result)
        return dict(result)
    except Exception as exc:
        logger.warning("MF NAV lookup failed (code=%s, name=%s): %s", code, name, exc)
        return None


def fetch_mf_historical_nav(
    amfi_code: str = "",
    scheme_name: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.Series:
    """Fetch historical daily NAV series for an Indian mutual fund via mfapi.in (with mftool fallback)."""
    code = str(amfi_code or "").strip()
    name = str(scheme_name or "").strip()
    if not code and name:
        mf = _get_mftool()
        if mf is not None:
            try:
                all_codes = mf.get_scheme_codes(as_json=False)
                if isinstance(all_codes, dict):
                    code = _best_scheme_match(name, all_codes) or ""
            except Exception as exc:
                logger.debug("Scheme code lookup failed for %s: %s", name, exc)
    if not code:
        return pd.Series(dtype=float)

    start_iso = pd.Timestamp(start_date).date().isoformat() if start_date else ""
    end_iso = pd.Timestamp(end_date).date().isoformat() if end_date else ""
    cache_key = ("mf_hist_nav", code)
    hit, cached = _CACHE.get(cache_key)
    full_series: pd.Series | None = cached.copy() if (hit and isinstance(cached, pd.Series)) else None

    if full_series is None:
        rows_list: list[tuple[pd.Timestamp, float]] = []
        try:
            import requests

            resp = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=12)
            if resp.status_code == 200:
                payload = resp.json()
                for item in payload.get("data", []) or []:
                    dt_str = str(item.get("date", "")).strip()
                    nav_str = str(item.get("nav", "")).strip()
                    if not dt_str or not nav_str:
                        continue
                    try:
                        dt = pd.to_datetime(dt_str, format="%d-%m-%Y", errors="coerce")
                        nav_val = float(nav_str.replace(",", ""))
                        if pd.notna(dt) and nav_val > 0:
                            rows_list.append((dt.normalize(), nav_val))
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("mfapi.in history failed for %s: %s", code, exc)

        if not rows_list:
            mf = _get_mftool()
            if mf is not None:
                try:
                    raw = mf.get_scheme_historical_nav(code, as_json=False) or {}
                    for item in raw.get("data", []) or []:
                        dt_str = str(item.get("date", "")).strip()
                        nav_str = str(item.get("nav", "")).strip()
                        try:
                            dt = pd.to_datetime(dt_str, format="%d-%m-%Y", errors="coerce")
                            nav_val = float(nav_str.replace(",", ""))
                            if pd.notna(dt) and nav_val > 0:
                                rows_list.append((dt.normalize(), nav_val))
                        except Exception:
                            continue
                except Exception as exc:
                    logger.debug("mftool history fallback failed for %s: %s", code, exc)

        if not rows_list:
            empty = pd.Series(dtype=float)
            _CACHE.set(cache_key, empty, ttl_seconds=120)
            return empty

        idx = pd.DatetimeIndex([r[0] for r in rows_list])
        full_series = _normalize_dt_index(pd.Series([r[1] for r in rows_list], index=idx, dtype=float))
        _CACHE.set(cache_key, full_series.copy())

    out = full_series
    if start_iso:
        out = out.loc[pd.Timestamp(start_iso) :]
    if end_iso:
        out = out.loc[: pd.Timestamp(end_iso)]
    return out


def fetch_stock_splits(
    symbol: str,
    start_date: str,
    db_path: Path | str | None = None,
) -> pd.Series:
    """Fetch corporate stock split factors from Yahoo Finance since `start_date`."""
    if not symbol or not str(symbol).strip():
        return pd.Series(dtype=float)

    start_ts = pd.Timestamp(start_date).normalize()
    ysym = resolve_yahoo_symbol(symbol, db_path=db_path)
    cache_key = ("splits", ysym, start_ts.date().isoformat())
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return cached.copy()

    try:
        splits = yf.Ticker(ysym).splits
        if splits is None or splits.empty:
            empty = pd.Series(dtype=float)
            _CACHE.set(cache_key, empty)
            return empty
        norm = _normalize_dt_index(pd.to_numeric(splits, errors="coerce").dropna())
        filtered = norm[(norm.index >= start_ts) & (norm > 0)]
        _CACHE.set(cache_key, filtered.copy())
        return filtered
    except Exception as exc:
        logger.debug("Failed to fetch splits for %s (%s): %s", symbol, ysym, exc)
        return pd.Series(dtype=float)
