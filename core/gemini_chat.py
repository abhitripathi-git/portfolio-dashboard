"""Embedded Gemini chat engine: fast local M5 pre-computation + parallel portfolio tools."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key

from config import BASE_DIR, BENCHMARKS, DB_PATH
from core.analytics import compare_portfolio_vs_benchmark, get_portfolio_snapshot
from core.db import (
    list_accounts,
    query_mf_holdings,
    query_other_assets,
    query_transactions,
    run_readonly_query,
)
from core.market_data import fetch_mf_nav

ENV_FILE_PATH = BASE_DIR / ".env"

AVAILABLE_GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]


def load_env_api_key() -> str:
    """Load GEMINI_API_KEY or GOOGLE_API_KEY from `.env` or environment."""
    if ENV_FILE_PATH.exists():
        load_dotenv(dotenv_path=ENV_FILE_PATH, override=False)
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def save_env_api_key(api_key: str) -> Path:
    """Persist GEMINI_API_KEY to local `.env` and update current process env."""
    clean = api_key.strip()
    if not ENV_FILE_PATH.exists():
        ENV_FILE_PATH.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE_PATH), "GEMINI_API_KEY", clean)
    os.environ["GEMINI_API_KEY"] = clean
    return ENV_FILE_PATH


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
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
    return str(value)


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


def _aggregate_locally(holdings: list[dict[str, Any]], field: str, default_val: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, float]] = {}
    total_curr = sum(float(h.get("current_value", 0.0)) for h in holdings)
    for h in holdings:
        key = str(h.get(field) or default_val).strip() or default_val
        g = groups.setdefault(key, {"positions": 0, "invested": 0.0, "current": 0.0})
        g["positions"] += 1
        g["invested"] += float(h.get("invested_value", 0.0))
        g["current"] += float(h.get("current_value", 0.0))
    out = []
    for key, g in sorted(groups.items(), key=lambda x: x[1]["current"], reverse=True):
        inv, cur = g["invested"], g["current"]
        pnl = cur - inv
        out.append(
            {
                field: key,
                "positions": int(g["positions"]),
                "invested": round(inv, 2),
                "current": round(cur, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((pnl / inv * 100.0) if inv > 0 else 0.0, 2),
                "weight_pct": round((cur / total_curr * 100.0) if total_curr > 0 else 0.0, 2),
            }
        )
    return out


def build_local_portfolio_context(
    active_owner: str | None,
    db_path: Path | str = DB_PATH,
    precomputed_snapshot: dict[str, Any] | None = None,
) -> str:
    """Compute a compact local portfolio digest on the Mac so Gemini avoids slow tool round-trips."""
    target_db = Path(db_path)
    owner_arg = (
        None
        if not active_owner or active_owner.strip().lower() in ("all", "combined", "both", "*")
        else active_owner.strip()
    )

    snap = precomputed_snapshot
    if snap is None:
        if not target_db.exists():
            return "{}"
        snap = get_portfolio_snapshot(owner=owner_arg, include_mf=True, db_path=target_db)

    holdings = snap.get("holdings", [])
    compact_holdings = [
        {
            "sym": h.get("symbol") if h.get("asset_class") == "EQUITY" else h.get("scheme_name"),
            "cls": "EQ" if h.get("asset_class") == "EQUITY" else "MF",
            "qty": h.get("quantity"),
            "avg": h.get("avg_price"),
            "ltp": h.get("current_price"),
            "inv": h.get("invested_value"),
            "val": h.get("current_value"),
            "pnl": h.get("unrealized_pnl"),
            "pnl_pct": h.get("unrealized_pnl_pct"),
            "day_pct": h.get("day_change_pct"),
            "wt": h.get("weight_pct"),
            "reg": h.get("region", "India"),
            "adv": h.get("advisory_source", "Self"),
        }
        for h in holdings
    ]

    by_pnl = sorted(holdings, key=lambda x: float(x.get("unrealized_pnl", 0.0)))
    losers = [
        {
            "sym": h.get("symbol") or h.get("scheme_name"),
            "pnl": h.get("unrealized_pnl"),
            "pnl_pct": h.get("unrealized_pnl_pct"),
            "adv": h.get("advisory_source", "Self"),
        }
        for h in by_pnl[:5]
    ]
    gainers = [
        {
            "sym": h.get("symbol") or h.get("scheme_name"),
            "pnl": h.get("unrealized_pnl"),
            "pnl_pct": h.get("unrealized_pnl_pct"),
            "adv": h.get("advisory_source", "Self"),
        }
        for h in reversed(by_pnl[-5:])
    ]

    tx_meta: dict[str, Any] = {}
    try:
        tx_rows = run_readonly_query(
            "SELECT COUNT(*) AS cnt, MIN(trade_date) AS min_dt, MAX(trade_date) AS max_dt FROM transactions",
            db_path=target_db,
        )
        if tx_rows:
            tx_meta = tx_rows[0]
    except Exception:
        pass

    other_assets = query_other_assets(owner=owner_arg, db_path=target_db)
    alt_invested = round(sum(float(a.get("invested_value") or 0.0) for a in other_assets), 2)
    alt_current = round(sum(float(a.get("current_value") or 0.0) for a in other_assets), 2)
    market_curr = float((snap.get("totals") or {}).get("current_value") or 0.0)

    digest = {
        "scope": snap.get("owner", owner_arg or "Combined"),
        "as_of": snap.get("as_of_date"),
        "totals": snap.get("totals", {}),
        "net_worth": {
            "market_current_value": round(market_curr, 2),
            "alternative_invested_value": alt_invested,
            "alternative_current_value": alt_current,
            "total_net_worth": round(market_curr + alt_current, 2),
        },
        "alternative_assets": [
            {
                "owner": a.get("owner"),
                "name": a.get("name"),
                "category": a.get("category"),
                "invested": a.get("invested_value"),
                "current": a.get("current_value"),
                "as_of": a.get("as_of_date"),
                "notes": a.get("notes"),
            }
            for a in other_assets
        ],
        "by_advisory": _aggregate_locally(holdings, "advisory_source", "Self"),
        "by_region": _aggregate_locally(holdings, "region", "India"),
        "top_gainers": gainers,
        "top_losers": losers,
        "tx_summary": tx_meta,
        "holdings": compact_holdings,
        "warnings": snap.get("warnings", []),
    }
    return json.dumps(_json_safe(digest), ensure_ascii=False, separators=(",", ":"))


def build_portfolio_tools(db_path: Path | str = DB_PATH) -> tuple[list[Any], dict[str, Any]]:
    """Create typed tool callables bound to `db_path` for Gemini function calling."""
    target_db = Path(db_path)

    def tool_list_portfolio_accounts(owner: str | None = None) -> dict[str, Any]:
        """List brokerage and mutual fund accounts in SQLite.

        Args:
            owner: Optional family member name ("Abhishek", "Tanu", or None for all).
        """
        accounts = list_accounts(owner=owner, db_path=target_db)
        return _json_safe({"count": len(accounts), "accounts": accounts})

    def tool_get_portfolio_summary(
        owner: str | None = None,
        exclude_symbols: list[str] | None = None,
        include_mf: bool = True,
    ) -> dict[str, Any]:
        """Get portfolio totals (invested, current value, P&L, day change, XIRR) and top holdings.

        Args:
            owner: "Abhishek", "Tanu", or None for combined family view.
            exclude_symbols: Optional list of stock symbols to exclude for what-if checks.
            include_mf: Include mutual funds alongside direct equities.
        """
        snap = get_portfolio_snapshot(
            owner=owner,
            exclude_symbols=exclude_symbols,
            include_mf=include_mf,
            db_path=target_db,
        )
        return _json_safe(
            {
                "owner": snap["owner"],
                "as_of_date": snap["as_of_date"],
                "excluded_symbols": snap["excluded_symbols"],
                "totals": snap["totals"],
                "by_advisory": _aggregate_locally(snap["holdings"], "advisory_source", "Self"),
                "by_region": _aggregate_locally(snap["holdings"], "region", "India"),
                "top_holdings": [
                    {k: v for k, v in h.items() if k != "extra_data"}
                    for h in snap["holdings"][:15]
                ],
                "warnings": snap["warnings"],
            }
        )

    def tool_get_holdings_detail(
        owner: str | None = None,
        exclude_symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get all direct equity holdings with EOD/live prices, avg cost, region, advisory, weight %, and P&L.

        Args:
            owner: "Abhishek", "Tanu", or None for combined.
            exclude_symbols: Optional stock symbols to exclude.
        """
        snap = get_portfolio_snapshot(
            owner=owner,
            exclude_symbols=exclude_symbols,
            include_mf=False,
            db_path=target_db,
        )
        return _json_safe(
            {
                "owner": snap["owner"],
                "as_of_date": snap["as_of_date"],
                "totals": snap["totals"],
                "equity_holdings": snap["equity_holdings"],
                "warnings": snap["warnings"],
            }
        )

    def tool_get_mf_holdings_detail(owner: str | None = None) -> dict[str, Any]:
        """Get all mutual fund holdings with latest AMFI NAVs, region, advisory, and P&L.

        Args:
            owner: "Abhishek", "Tanu", or None for combined.
        """
        rows = query_mf_holdings(owner=owner, db_path=target_db)
        out = []
        for m in rows:
            nav_info = fetch_mf_nav(
                amfi_code=str(m.get("amfi_code", "")).strip(),
                scheme_name=str(m.get("scheme_name", "")).strip(),
            )
            units = float(m.get("units", 0.0))
            invested = float(m.get("invested_value") or round(units * float(m.get("avg_nav", 0.0)), 2))
            curr_nav = float(nav_info["nav"]) if nav_info and nav_info.get("nav") is not None else None
            curr_val = units * curr_nav if curr_nav is not None else invested
            out.append(
                {
                    "owner": m.get("owner"),
                    "scheme_name": m.get("scheme_name"),
                    "folio_number": m.get("folio_number"),
                    "region": m.get("region", "India"),
                    "advisory_source": m.get("advisory_source", "Self"),
                    "units": units,
                    "invested_value": round(invested, 2),
                    "latest_nav": curr_nav,
                    "current_value": round(curr_val, 2),
                    "pnl": round(curr_val - invested, 2),
                }
            )
        return _json_safe({"count": len(out), "mf_holdings": out})

    def tool_get_transactions_history(
        owner: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Fetch historical buy/sell/split/bonus/dividend transactions.

        Args:
            owner: "Abhishek", "Tanu", or None for combined.
            start_date: Inclusive YYYY-MM-DD start date.
            end_date: Inclusive YYYY-MM-DD end date.
            symbol: Optional exact scrip/symbol name filter.
        """
        txs = query_transactions(
            owner=owner,
            start_date=start_date,
            end_date=end_date,
            symbol=symbol,
            db_path=target_db,
        )
        return _json_safe({"count": len(txs), "transactions": txs[-200:]})

    def tool_compare_against_benchmark(
        owner: str | None = None,
        benchmark: str = "NIFTY_MIDCAP_150",
        start_date: str | None = None,
        end_date: str | None = None,
        exclude_symbols: list[str] | None = None,
        region: str | None = None,
        advisory_source: str | None = None,
    ) -> dict[str, Any]:
        """Compare equity portfolio returns vs an Indian index benchmark over a date range.

        Args:
            owner: "Abhishek", "Tanu", or None for combined.
            benchmark: One of NIFTY_50, NIFTY_MIDCAP_150, NIFTY_MIDCAP_50, NIFTY_SMALLCAP_250, NIFTY_NEXT_50, SENSEX.
            start_date: Optional YYYY-MM-DD start date.
            end_date: Optional YYYY-MM-DD end date.
            exclude_symbols: Optional list of scrip names to exclude before comparing.
            region: Optional region filter ("India" or "Foreign").
            advisory_source: Optional advisory filter (e.g. "Self", "RC Capital", "Fintrekk Capital").
        """
        bench_key = _normalize_benchmark_key(benchmark)
        res = compare_portfolio_vs_benchmark(
            owner=owner,
            benchmark_key=bench_key,
            start_date=start_date,
            end_date=end_date,
            exclude_symbols=exclude_symbols,
            adjust_for_splits=True,
            region=region,
            advisory_source=advisory_source,
            db_path=target_db,
        )
        return _json_safe(
            {
                "summary": res["summary"],
                "included_count": len(res["included_symbols"]),
                "excluded_symbols": res["excluded_symbols"],
                "warnings": res["warnings"],
            }
        )

    def tool_run_readonly_sql(sql: str) -> dict[str, Any]:
        """Execute a read-only SQLite SELECT/WITH/PRAGMA query against the portfolio database.

        Tables:
        - accounts(id, owner, broker, account_name, account_code, extra_data, created_at)
        - transactions(id, account_id, trade_date, symbol, isin, exchange, asset_type, side, quantity, price, amount, fees, source, region, advisory_source, advisory_notes, extra_data, created_at)
        - holdings(id, account_id, symbol, isin, company_name, exchange, asset_type, quantity, avg_price, invested_value, as_of_date, source, region, advisory_source, advisory_notes, extra_data, updated_at)
        - holding_baselines(id, account_id, symbol, isin, company_name, exchange, asset_type, quantity, avg_price, invested_value, as_of_date, source, region, advisory_source, advisory_notes, extra_data, updated_at)
        - mf_holdings(id, account_id, folio_number, scheme_name, amfi_code, isin, units, avg_nav, invested_value, as_of_date, source, region, advisory_source, advisory_notes, extra_data, updated_at)
        - other_assets(id, owner, name, category, invested_value, current_value, as_of_date, notes, extra_data, updated_at)
        - symbol_aliases(raw_symbol, yahoo_symbol, isin, company_name, sector, updated_at)
        """
        rows = run_readonly_query(sql, db_path=target_db)
        return _json_safe({"row_count": len(rows), "rows": rows[:200]})

    tools = [
        tool_list_portfolio_accounts,
        tool_get_portfolio_summary,
        tool_get_holdings_detail,
        tool_get_mf_holdings_detail,
        tool_get_transactions_history,
        tool_compare_against_benchmark,
        tool_run_readonly_sql,
    ]
    tool_map = {fn.__name__: fn for fn in tools}
    return tools, tool_map


def ask_portfolio_gemini(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    api_key: str = "",
    model: str = "gemini-3.6-flash",
    active_owner: str | None = None,
    db_path: Path | str = DB_PATH,
    max_tool_rounds: int = 8,
    precomputed_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a fast hybrid local+cloud Gemini query against the local portfolio DB."""
    if not api_key or not api_key.strip():
        raise ValueError("Missing Gemini API key.")

    from google import genai
    from google.genai import types

    tools, tool_map = build_portfolio_tools(db_path=db_path)
    owner_hint = active_owner or "Combined (Abhishek & Tanu)"

    try:
        local_digest = build_local_portfolio_context(
            active_owner=active_owner,
            db_path=db_path,
            precomputed_snapshot=precomputed_snapshot,
        )
    except Exception:
        local_digest = "{}"

    system_instruction = (
        "You are a fast, precise Indian stock & mutual fund portfolio analyst for Abhishek and Tanu. "
        f"Active dashboard scope: {owner_hint}. Today's date: {date.today().isoformat()}.\n\n"
        "HYBRID EXECUTION RULE (IMPORTANT FOR SPEED):\n"
        "- The JSON below was ALREADY pre-computed locally on the user's Mac from SQLite + live prices.\n"
        "- For summaries, current values, P&L, top/worst holdings, India vs Foreign breakdown, Advisory breakdown "
        "(Self, RC Capital, Fintrekk Capital), Alternative & Fixed Assets (NPS/EPFO/PPF/Real Estate), and Total Net Worth, "
        "answer DIRECTLY from this local snapshot in ONE turn without calling tools.\n"
        "- Only invoke tools when you need something NOT in the snapshot: date-window benchmark simulations "
        "(tool_compare_against_benchmark, which also supports region and advisory_source), individual trades "
        "(tool_get_transactions_history), what-if exclusions, or another owner/account outside the active scope.\n"
        "- For mutual fund overlap or factsheet composition questions, use the exact mutual fund scheme names, "
        "AMFI codes, and direct equity symbols already provided in LOCAL_PORTFOLIO_DIGEST (or tool_get_mf_holdings_detail) "
        "and perform the overlap & sector analysis directly in the cloud using live factsheet knowledge.\n"
        "- Format INR amounts cleanly (e.g., ₹12.45 L or ₹1,24,500) and mention any warnings transparently.\n\n"
        f"LOCAL_PORTFOLIO_DIGEST={local_digest}"
    )

    contents: list[types.Content] = []
    for msg in (history or [])[-10:]:
        role = "model" if msg.get("role") == "assistant" else "user"
        text = str(msg.get("content", "")).strip()
        if text:
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

    client = genai.Client(api_key=api_key.strip())
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
    )

    candidate_models = [model] + [
        m for m in ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash") if m != model
    ]

    def _call_with_fallback(active_model: str, req_contents: list[types.Content]) -> tuple[Any, str]:
        last_exc: Exception | None = None
        order = [active_model] + [m for m in candidate_models if m != active_model]
        for idx, cand in enumerate(order):
            try:
                return client.models.generate_content(model=cand, contents=req_contents, config=config), cand
            except Exception as exc:
                msg = str(exc)
                is_transient = ("503" in msg or "429" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg)
                if is_transient and idx < len(order) - 1:
                    last_exc = exc
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    def _exec_tool(fn_name: str, fn_args: dict[str, Any]) -> dict[str, Any]:
        fn = tool_map.get(fn_name)
        if fn is None:
            return {"error": f"Unknown tool: {fn_name}"}
        try:
            return fn(**fn_args)
        except Exception as exc:
            return {"error": str(exc)}

    tool_trace: list[dict[str, Any]] = []
    response, used_model = _call_with_fallback(model, contents)

    for _ in range(max_tool_rounds):
        fcalls = getattr(response, "function_calls", None) or []
        if not fcalls:
            break

        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        call_items = [(fc.name, dict(fc.args or {})) for fc in fcalls]
        for fn_name, fn_args in call_items:
            tool_trace.append({"tool": fn_name, "args": fn_args})

        if len(call_items) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(call_items))) as pool:
                results = list(pool.map(lambda item: _exec_tool(item[0], item[1]), call_items))
        else:
            results = [_exec_tool(call_items[0][0], call_items[0][1])]

        response_parts = [
            types.Part.from_function_response(name=fn_name, response={"result": res})
            for (fn_name, _), res in zip(call_items, results)
        ]

        contents.append(types.Content(role="user", parts=response_parts))
        response, used_model = _call_with_fallback(used_model, contents)

    reply_text = (getattr(response, "text", None) or "").strip()
    if not reply_text:
        reply_text = "I queried your portfolio tools, but Gemini returned an empty text response. Please try rephrasing."

    return {
        "reply": reply_text,
        "tools_used": tool_trace,
        "model": used_model,
    }
