"""Mutual Fund vs. Direct Stock and MF-vs-MF portfolio overlap analysis."""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from config import DATA_DIR
from core.analytics import get_portfolio_snapshot
from core.db import query_mf_holdings
from core.market_data import _CACHE, _get_mftool

logger = logging.getLogger(__name__)

CONSTITUENTS_FILE = DATA_DIR / "mf_constituents.json"

# Maps normalized scheme-name patterns to official NSE index names for live constituent lookup
_NSE_INDEX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bnifty\s*next\s*50\b", re.I), "NIFTY NEXT 50"),
    (re.compile(r"\bnifty\s*midcap\s*150\b", re.I), "NIFTY MIDCAP 150"),
    (re.compile(r"\bnifty\s*midcap\s*100\b", re.I), "NIFTY MIDCAP 100"),
    (re.compile(r"\bnifty\s*midcap\s*50\b", re.I), "NIFTY MIDCAP 50"),
    (re.compile(r"\bnifty\s*smallcap\s*250\b", re.I), "NIFTY SMALLCAP 250"),
    (re.compile(r"\bnifty\s*smallcap\s*100\b", re.I), "NIFTY SMALLCAP 100"),
    (re.compile(r"\bnifty\s*largemidcap\s*250\b", re.I), "NIFTY LARGEMIDCAP 250"),
    (re.compile(r"\bnifty\s*500\b", re.I), "NIFTY 500"),
    (re.compile(r"\bnifty\s*100\b", re.I), "NIFTY 100"),
    (re.compile(r"\bnifty\s*bank\b|\bbank\s*bees\b", re.I), "NIFTY BANK"),
    (re.compile(r"\bnifty\s*it\b", re.I), "NIFTY IT"),
    (re.compile(r"\bnifty\s*50\b|\bniftybees\b", re.I), "NIFTY 50"),
]

_NON_EQUITY_RE = re.compile(
    r"\b(liquid|overnight|money market|gilt|corporate bond|banking\s*&?\s*psu|"
    r"dynamic bond|short duration|medium duration|long duration|ultra short|"
    r"low duration|credit risk|floater|debt|gold|silver|target maturity)\b",
    re.I,
)

_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\blarge\s*&?\s*mid\s*cap\b", re.I), "Large & Mid Cap"),
    (re.compile(r"\bflexi\s*cap\b", re.I), "Flexi Cap"),
    (re.compile(r"\bmulti\s*cap\b", re.I), "Multi Cap"),
    (re.compile(r"\bmid\s*cap\b", re.I), "Mid Cap"),
    (re.compile(r"\bsmall\s*cap\b", re.I), "Small Cap"),
    (re.compile(r"\blarge\s*cap\b|\bbluechip\b|\btop\s*100\b", re.I), "Large Cap"),
    (re.compile(r"\belss\b|\btax\s*saver\b", re.I), "ELSS"),
    (re.compile(r"\bfocused\b", re.I), "Focused"),
    (re.compile(r"\bvalue\b|\bcontra\b", re.I), "Value / Contra"),
    (re.compile(r"\bindex\b|\betf\b|\bnifty\b|\bsensex\b", re.I), "Index / ETF"),
    (re.compile(r"\bbalanced\b|\bhybrid\b|\baggressive\b", re.I), "Hybrid"),
    (re.compile(r"\barbitrage\b", re.I), "Arbitrage"),
]


def _detect_nse_index_name(scheme_name: str) -> str | None:
    for pattern, idx_name in _NSE_INDEX_PATTERNS:
        if pattern.search(scheme_name):
            return idx_name
    return None


def _infer_category(scheme_name: str, api_category: str = "") -> str:
    combined = f"{api_category} {scheme_name}".strip()
    if _NON_EQUITY_RE.search(combined):
        return "Debt / Non-Equity"
    for pattern, cat in _CATEGORY_RULES:
        if pattern.search(combined):
            return cat
    return api_category.strip() or "Diversified Equity"


def _fetch_scheme_metadata(amfi_code: str) -> dict[str, Any]:
    """Fetch scheme category & fund house from mftool if amfi_code is available."""
    code = str(amfi_code or "").strip()
    if not code:
        return {}
    cache_key = ("mf_meta", code)
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return dict(cached)

    mf = _get_mftool()
    if mf is None:
        return {}
    try:
        details = mf.get_scheme_details(code)
        if isinstance(details, dict) and details:
            _CACHE.set(cache_key, details)
            return dict(details)
    except Exception as exc:
        logger.debug("get_scheme_details failed for %s: %s", code, exc)
    return {}


def _fetch_nse_index_constituents(index_name: str) -> dict[str, float]:
    """Attempt on-demand live index constituent fetch from NSE India API."""
    cache_key = ("nse_index", index_name)
    hit, cached = _CACHE.get(cache_key)
    if hit:
        return dict(cached)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        url = f"https://www.nseindia.com/api/equity-stockIndices?index={quote(index_name)}"
        resp = session.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []

        raw_weights: dict[str, float] = {}
        for row in rows:
            sym = str(row.get("symbol", "")).strip().upper()
            if not sym or sym == index_name.upper() or "NIFTY" in sym:
                continue
            ffmc = row.get("ffmc")
            if ffmc is not None:
                try:
                    val = float(ffmc)
                    if val > 0:
                        raw_weights[sym] = val
                except (TypeError, ValueError):
                    continue

        total_ffmc = sum(raw_weights.values())
        if total_ffmc <= 0:
            _CACHE.set(cache_key, {}, ttl_seconds=300)
            return {}

        weights = {sym: round(val / total_ffmc * 100.0, 4) for sym, val in raw_weights.items()}
        _CACHE.set(cache_key, weights)
        return dict(weights)
    except Exception as exc:
        logger.debug("NSE index constituent fetch failed for %s: %s", index_name, exc)
        _CACHE.set(cache_key, {}, ttl_seconds=180)
        return {}


def _load_local_constituents_map() -> dict[str, Any]:
    """Load optional user-provided data/mf_constituents.json if it exists."""
    if not CONSTITUENTS_FILE.exists():
        return {}
    try:
        raw = json.loads(CONSTITUENTS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", CONSTITUENTS_FILE, exc)
        return {}


def _normalize_constituent_weights(raw_map: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, wt in raw_map.items():
        clean_sym = str(sym or "").strip().upper()
        if not clean_sym:
            continue
        try:
            w = float(wt)
            if w > 0:
                out[clean_sym] = round(w, 4)
        except (TypeError, ValueError):
            continue
    return out


def _parse_constituents_obj(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _resolve_fund_constituents(
    scheme_name: str,
    amfi_code: str,
    extra_data: dict[str, Any],
    local_map: dict[str, Any],
) -> dict[str, Any]:
    """Resolve underlying stock weights for a mutual fund with explicit provenance."""
    # 1. Explicit constituents stored in mf_holdings.extra_data
    if isinstance(extra_data, dict):
        parsed_extra = _parse_constituents_obj(extra_data.get("constituents"))
        if parsed_extra:
            weights = _normalize_constituent_weights(parsed_extra)
            if weights:
                return {
                    "constituents": weights,
                    "data_source": str(extra_data.get("constituents_source", "extra_data")),
                    "confidence": str(extra_data.get("constituents_confidence", "high")),
                    "category": _infer_category(scheme_name, str(extra_data.get("category", ""))),
                    "is_equity_fund": True,
                }

    # 2. User-provided local constituents JSON (data/mf_constituents.json)
    for key in (amfi_code, scheme_name, scheme_name.lower()):
        if key and key in local_map:
            entry = local_map[key]
            if isinstance(entry, dict):
                raw_c = entry.get("constituents", entry)
                if isinstance(raw_c, dict):
                    weights = _normalize_constituent_weights(raw_c)
                    if weights:
                        return {
                            "constituents": weights,
                            "data_source": str(entry.get("data_source", "local_scheme_constituents")),
                            "confidence": str(entry.get("confidence", "high")),
                            "category": _infer_category(scheme_name, str(entry.get("category", ""))),
                            "is_equity_fund": True,
                        }

    meta = _fetch_scheme_metadata(amfi_code)
    api_category = str(meta.get("scheme_category", ""))
    category = _infer_category(scheme_name, api_category)

    # 3. Non-equity funds (Debt, Liquid, Gold, etc.)
    if category == "Debt / Non-Equity":
        return {
            "constituents": {},
            "data_source": "scheme_metadata",
            "confidence": "high",
            "category": category,
            "is_equity_fund": False,
        }

    # 4. On-demand NSE Index constituent lookup for Index Funds / ETFs
    nse_idx = _detect_nse_index_name(scheme_name)
    if nse_idx:
        weights = _fetch_nse_index_constituents(nse_idx)
        if weights:
            return {
                "constituents": weights,
                "data_source": f"nse_index_live:{nse_idx}",
                "confidence": "high",
                "category": category,
                "is_equity_fund": True,
            }

    # 5. Active / unmapped fund: do NOT fabricate stock weights
    return {
        "constituents": {},
        "data_source": "unavailable",
        "confidence": "none",
        "category": category,
        "is_equity_fund": True,
    }


def analyze_mf_stock_overlap(
    owner: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Analyze Direct Stock vs. Mutual Fund overlap and MF-vs-MF overlap matrix."""
    snapshot = get_portfolio_snapshot(owner=owner, include_mf=True, db_path=db_path)
    raw_mf_rows = query_mf_holdings(owner=owner, db_path=db_path)

    # Build map of extra_data from raw DB rows keyed by (amfi_code, scheme_name)
    extra_by_scheme: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_mf_rows:
        key = (str(row.get("amfi_code", "")).strip(), str(row.get("scheme_name", "")).strip())
        if isinstance(row.get("extra_data"), dict):
            extra_by_scheme.setdefault(key, {}).update(row["extra_data"])

    # Aggregate direct equity holdings by symbol
    direct_by_symbol: dict[str, float] = {}
    for h in snapshot["equity_holdings"]:
        sym = str(h["symbol"]).strip().upper()
        direct_by_symbol[sym] = direct_by_symbol.get(sym, 0.0) + float(h["current_value"])

    # Aggregate MF holdings by scheme_name (preserving resolved amfi_code)
    mf_agg: dict[str, dict[str, Any]] = {}
    for m in snapshot["mf_holdings"]:
        sname = str(m["scheme_name"]).strip()
        acode = str(m.get("amfi_code", "")).strip()
        entry = mf_agg.setdefault(
            sname,
            {
                "scheme_name": sname,
                "amfi_code": acode,
                "current_value": 0.0,
                "invested_value": 0.0,
                "extra_data": extra_by_scheme.get((acode, sname), {})
                or extra_by_scheme.get(("", sname), {}),
            },
        )
        if acode and not entry["amfi_code"]:
            entry["amfi_code"] = acode
        entry["current_value"] += float(m["current_value"])
        entry["invested_value"] += float(m["invested_value"])

    total_direct_val = sum(direct_by_symbol.values())
    total_mf_val = sum(v["current_value"] for v in mf_agg.values())
    total_portfolio_val = total_direct_val + total_mf_val

    local_map = _load_local_constituents_map()
    warnings: list[str] = list(snapshot.get("warnings", []))

    mf_profiles: list[dict[str, Any]] = []
    unmapped_equity_funds: list[str] = []
    category_counts: dict[str, list[str]] = {}

    for sname, info in sorted(mf_agg.items(), key=lambda x: x[1]["current_value"], reverse=True):
        resolved = _resolve_fund_constituents(
            scheme_name=sname,
            amfi_code=info["amfi_code"],
            extra_data=info["extra_data"],
            local_map=local_map,
        )
        constituents: dict[str, float] = resolved["constituents"]
        profile = {
            "scheme_name": sname,
            "amfi_code": info["amfi_code"],
            "current_value": round(info["current_value"], 2),
            "invested_value": round(info["invested_value"], 2),
            "category": resolved["category"],
            "is_equity_fund": resolved["is_equity_fund"],
            "data_source": resolved["data_source"],
            "confidence": resolved["confidence"],
            "constituents_count": len(constituents),
            "constituents": constituents,
        }
        mf_profiles.append(profile)

        if resolved["is_equity_fund"]:
            category_counts.setdefault(resolved["category"], []).append(sname)
            if not constituents:
                unmapped_equity_funds.append(sname)

    if unmapped_equity_funds:
        warnings.append(
            f"Exact stock constituents unavailable via AMFI/mftool for {len(unmapped_equity_funds)} "
            f"active equity fund(s): {', '.join(unmapped_equity_funds)}. "
            f"Add scheme portfolios to {CONSTITUENTS_FILE.name} for exact active-fund overlap."
        )

    for cat, funds in category_counts.items():
        if len(funds) >= 2 and cat != "Debt / Non-Equity":
            warnings.append(
                f"Multiple funds ({len(funds)}) in '{cat}' category ({', '.join(funds)}) — "
                "likely style/portfolio overlap (category_metadata, confidence=low)."
            )

    # 1. Direct Stock vs. Mutual Fund overlap & Combined Effective Exposure
    mf_implied_by_symbol: dict[str, float] = {}
    mf_sources_by_symbol: dict[str, list[dict[str, Any]]] = {}

    for prof in mf_profiles:
        fund_val = prof["current_value"]
        for sym, wt_pct in prof["constituents"].items():
            implied_val = fund_val * (wt_pct / 100.0)
            mf_implied_by_symbol[sym] = mf_implied_by_symbol.get(sym, 0.0) + implied_val
            mf_sources_by_symbol.setdefault(sym, []).append(
                {
                    "scheme_name": prof["scheme_name"],
                    "amfi_code": prof["amfi_code"],
                    "weight_in_fund_pct": round(wt_pct, 2),
                    "implied_value": round(implied_val, 2),
                    "data_source": prof["data_source"],
                    "confidence": prof["confidence"],
                }
            )

    all_symbols = sorted(set(direct_by_symbol.keys()) | set(mf_implied_by_symbol.keys()))
    direct_vs_mf: list[dict[str, Any]] = []
    effective_stock_exposure: list[dict[str, Any]] = []

    for sym in all_symbols:
        d_val = direct_by_symbol.get(sym, 0.0)
        m_val = mf_implied_by_symbol.get(sym, 0.0)
        tot_val = d_val + m_val
        eff_wt = (tot_val / total_portfolio_val * 100.0) if total_portfolio_val > 0 else 0.0
        d_wt = (d_val / total_portfolio_val * 100.0) if total_portfolio_val > 0 else 0.0
        sources = sorted(mf_sources_by_symbol.get(sym, []), key=lambda x: x["implied_value"], reverse=True)

        effective_stock_exposure.append(
            {
                "symbol": sym,
                "direct_value": round(d_val, 2),
                "mf_implied_value": round(m_val, 2),
                "total_value": round(tot_val, 2),
                "portfolio_weight_pct": round(eff_wt, 2),
                "in_direct": d_val > 0,
                "in_mf_count": len(sources),
            }
        )

        if d_val > 0 and sources:
            direct_vs_mf.append(
                {
                    "symbol": sym,
                    "direct_value": round(d_val, 2),
                    "direct_weight_pct": round(d_wt, 2),
                    "mf_implied_value": round(m_val, 2),
                    "total_effective_value": round(tot_val, 2),
                    "effective_weight_pct": round(eff_wt, 2),
                    "holding_funds": sources,
                }
            )

        if eff_wt >= 10.0 and total_portfolio_val > 0:
            warnings.append(
                f"High combined concentration in {sym}: {eff_wt:.1f}% of total portfolio "
                f"(direct ₹{d_val:,.0f} + MF implied ₹{m_val:,.0f})."
            )

    direct_vs_mf.sort(key=lambda x: x["total_effective_value"], reverse=True)
    effective_stock_exposure.sort(key=lambda x: x["total_value"], reverse=True)

    if direct_vs_mf:
        dup_syms = [item["symbol"] for item in direct_vs_mf[:5]]
        warnings.append(
            f"Direct stock overlap detected with MFs for {len(direct_vs_mf)} stock(s): "
            f"{', '.join(dup_syms)}{'...' if len(direct_vs_mf) > 5 else ''}."
        )

    # 2. Mutual Fund vs. Mutual Fund overlap matrix
    scheme_names = [p["scheme_name"] for p in mf_profiles]
    matrix: dict[str, dict[str, float | None]] = {s: {} for s in scheme_names}
    pairs: list[dict[str, Any]] = []

    for i, pa in enumerate(mf_profiles):
        sa = pa["scheme_name"]
        ca = pa["constituents"]
        for j, pb in enumerate(mf_profiles):
            sb = pb["scheme_name"]
            cb = pb["constituents"]
            if i == j:
                matrix[sa][sb] = 100.0 if ca else None
                continue
            if ca and cb:
                common = sorted(
                    set(ca.keys()) & set(cb.keys()),
                    key=lambda sym: min(ca[sym], cb[sym]),
                    reverse=True,
                )
                overlap_pct = round(sum(min(ca[sym], cb[sym]) for sym in common), 2)
                matrix[sa][sb] = overlap_pct
                if i < j:
                    pair_conf = "high" if (pa["confidence"] == "high" and pb["confidence"] == "high") else "medium"
                    pairs.append(
                        {
                            "scheme_a": sa,
                            "scheme_b": sb,
                            "overlap_pct": overlap_pct,
                            "common_stocks": common,
                            "common_count": len(common),
                            "confidence": pair_conf,
                            "note": "",
                        }
                    )
                    if overlap_pct >= 40.0:
                        warnings.append(
                            f"High MF overlap ({overlap_pct:.1f}%) between '{sa}' and '{sb}'."
                        )
            else:
                matrix[sa][sb] = None
                if i < j:
                    same_cat = (
                        pa["is_equity_fund"]
                        and pb["is_equity_fund"]
                        and pa["category"] == pb["category"]
                    )
                    pairs.append(
                        {
                            "scheme_a": sa,
                            "scheme_b": sb,
                            "overlap_pct": None,
                            "common_stocks": [],
                            "common_count": 0,
                            "confidence": "none",
                            "note": (
                                f"Both in '{pa['category']}' category (exact constituents unavailable)"
                                if same_cat
                                else "Constituent data unavailable for one or both funds"
                            ),
                        }
                    )

    funds_with_data = sum(1 for p in mf_profiles if p["constituents"])
    funds_without_data = sum(1 for p in mf_profiles if p["is_equity_fund"] and not p["constituents"])

    return {
        "summary": {
            "owner": snapshot["owner"],
            "total_direct_equity_value": round(total_direct_val, 2),
            "total_mf_value": round(total_mf_val, 2),
            "total_portfolio_value": round(total_portfolio_val, 2),
            "overlapping_direct_stocks_count": len(direct_vs_mf),
            "mf_count": len(mf_profiles),
            "funds_with_constituents": funds_with_data,
            "funds_without_constituents": funds_without_data,
        },
        "direct_vs_mf": direct_vs_mf,
        "mf_overlap_matrix": {
            "schemes": scheme_names,
            "matrix": matrix,
            "pairs": pairs,
        },
        "effective_stock_exposure": effective_stock_exposure,
        "mf_profiles": [
            {k: v for k, v in p.items() if k != "constituents"}
            for p in mf_profiles
        ],
        "warnings": list(dict.fromkeys(warnings)),
    }
