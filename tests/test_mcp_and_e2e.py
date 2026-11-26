"""Comprehensive tests for seed_demo.py and all mcp_server.py FastMCP tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import mcp_server
from core.db import get_symbol_aliases
from core.market_data import clear_market_cache, resolve_yahoo_symbol
from scripts.seed_demo import main as seed_cli_main
from scripts.seed_demo import seed_demo_data

_MOCK_EQUITY_PRICES: dict[str, tuple[float, float]] = {
    "RELIANCE.NS": (1320.0, 1345.0),
    "TCS.NS": (4100.0, 4180.0),
    "TATAMOTORS.NS": (890.0, 910.0),
    "HDFCBANK.NS": (1690.0, 1720.0),
    "INFY.NS": (1810.0, 1850.0),
    "PERSISTENT.NS": (5400.0, 5550.0),
    "ICICIBANK.NS": (1260.0, 1285.0),
    "ITC.NS": (475.0, 482.0),
    "SBIN.NS": (815.0, 830.0),
    "PIDILITIND.NS": (3150.0, 3210.0),
    "WIPRO.NS": (570.0, 585.0),
}

_MOCK_MF_QUOTES: dict[str, dict[str, str]] = {
    "122639": {
        "scheme_code": "122639",
        "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "nav": "84.50",
        "last_updated": "20-Nov-2026",
    },
    "120716": {
        "scheme_code": "120716",
        "scheme_name": "UTI Nifty 50 Index Fund - Direct Plan - Growth",
        "nav": "168.25",
        "last_updated": "20-Nov-2026",
    },
    "118989": {
        "scheme_code": "118989",
        "scheme_name": "HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth",
        "nav": "192.40",
        "last_updated": "20-Nov-2026",
    },
    "118825": {
        "scheme_code": "118825",
        "scheme_name": "Mirae Asset Large Cap Fund - Direct Plan - Growth",
        "nav": "115.80",
        "last_updated": "20-Nov-2026",
    },
    "119091": {
        "scheme_code": "119091",
        "scheme_name": "HDFC Liquid Fund - Direct Plan - Growth",
        "nav": "4920.10",
        "last_updated": "20-Nov-2026",
    },
}


def _assert_json_serializable(payload: object) -> None:
    dumped = json.dumps(payload, allow_nan=False)
    assert isinstance(dumped, str)


def _fake_yf_download(tickers, period=None, start=None, end=None, **kwargs) -> pd.DataFrame:
    ticker_list = [tickers] if isinstance(tickers, str) else list(tickers)

    if period == "5d":
        idx = pd.to_datetime(["2026-11-24", "2026-11-25"])
        data = {}
        for t in ticker_list:
            prev_px, curr_px = _MOCK_EQUITY_PRICES.get(t, (100.0, 102.0))
            data[("Close", t)] = [prev_px, curr_px]
        return pd.DataFrame(data, index=idx)

    s_dt = pd.Timestamp(start or "2024-01-01")
    e_dt = pd.Timestamp(end or "2024-07-01") - pd.Timedelta(days=1)
    if e_dt < s_dt:
        e_dt = s_dt
    dates = pd.date_range(s_dt, e_dt, freq="B")
    if len(dates) == 0:
        dates = pd.DatetimeIndex([s_dt])

    data = {}
    for t in ticker_list:
        if t in ("NIFTYMIDCAP150.NS", "MIDCAPETF.NS", "^CRSLDX", "^NSEI", "^BSESN"):
            vals = [18000.0 + i * 25.0 for i in range(len(dates))]
        else:
            _, curr_px = _MOCK_EQUITY_PRICES.get(t, (100.0, 105.0))
            base = curr_px * 0.8
            step = (curr_px - base) / max(len(dates) - 1, 1)
            vals = [round(base + i * step, 2) for i in range(len(dates))]
        data[("Close", t)] = vals
    return pd.DataFrame(data, index=dates)


def _fake_yf_ticker(symbol: str) -> MagicMock:
    mock_t = MagicMock()
    if symbol == "RELIANCE.NS":
        mock_t.splits = pd.Series([2.0], index=pd.to_datetime(["2024-10-28"]))
    elif symbol == "PERSISTENT.NS":
        mock_t.splits = pd.Series([2.0], index=pd.to_datetime(["2024-04-01"]))
    else:
        mock_t.splits = pd.Series(dtype=float)
    mock_t.history.return_value = pd.DataFrame()
    return mock_t


def _fake_mftool() -> MagicMock:
    mf = MagicMock()
    mf.get_scheme_quote.side_effect = lambda code: _MOCK_MF_QUOTES.get(str(code).strip())
    mf.get_scheme_details.side_effect = lambda code: {
        "scheme_code": str(code),
        "scheme_category": "Debt Scheme - Liquid Fund" if str(code) == "119091" else "Equity Scheme",
    }
    mf.get_scheme_codes.return_value = {
        k: v["scheme_name"] for k, v in _MOCK_MF_QUOTES.items()
    }
    return mf


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_market_cache()
    yield
    clear_market_cache()


@pytest.fixture()
def seeded_mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "portfolio_test.db"
    seed_demo_data(db_path=db_path, reset=True)
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path)
    return db_path


@pytest.fixture()
def mock_market():
    with (
        patch("core.market_data.yf.download", side_effect=_fake_yf_download),
        patch("core.market_data.yf.Ticker", side_effect=_fake_yf_ticker),
        patch("core.market_data._get_mftool", side_effect=_fake_mftool),
    ):
        yield


def test_seed_demo_data_idempotent_reset_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = tmp_path / "seed_cli.db"
    first = seed_demo_data(db_path=db_path, reset=True)
    assert first["owners"] == ["Abhishek", "Tanu"]
    assert first["transactions"]["Abhishek"]["inserted"] >= 10
    assert first["transactions"]["Tanu"]["inserted"] >= 6
    assert first["holdings"]["Abhishek"]["upserted"] == 7
    assert first["holdings"]["Tanu"]["upserted"] == 5
    assert first["mf_holdings"]["Abhishek"]["upserted"] == 3
    assert first["mf_holdings"]["Tanu"]["upserted"] == 3

    # Re-running without reset deduplicates transactions
    second = seed_demo_data(db_path=db_path, reset=False)
    assert second["transactions"]["Abhishek"]["inserted"] == 0
    assert second["transactions"]["Abhishek"]["skipped"] == first["transactions"]["Abhishek"]["inserted"]

    # CLI entry point with --reset
    with patch("sys.argv", ["seed_demo.py", "--db-path", str(db_path), "--reset"]):
        seed_cli_main()
    out = json.loads(capsys.readouterr().out)
    assert out["reset"] is True
    assert out["transactions"]["Abhishek"]["inserted"] == first["transactions"]["Abhishek"]["inserted"]


def test_mcp_server_registers_all_expected_tools():
    assert mcp_server.mcp.name == "personal-portfolio"
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "list_portfolio_accounts",
        "get_portfolio_summary",
        "get_holdings_detail",
        "get_mf_holdings_detail",
        "get_transactions_history",
        "compare_against_benchmark",
        "check_mf_stock_overlap",
        "add_trade_delta",
        "add_symbol_mapping",
        "run_readonly_sql",
    }
    assert expected.issubset(names)


def test_mcp_list_accounts_and_summary_owner_and_exclusions(seeded_mcp_db: Path, mock_market):
    all_acc = mcp_server.list_portfolio_accounts()
    _assert_json_serializable(all_acc)
    assert all_acc["owner"] == "Combined"
    assert all_acc["count"] == 2

    tanu_acc = mcp_server.list_portfolio_accounts(owner="tanu")
    _assert_json_serializable(tanu_acc)
    assert tanu_acc["owner"] == "Tanu"
    assert tanu_acc["count"] == 1
    assert tanu_acc["accounts"][0]["owner"] == "Tanu"

    combined_summary = mcp_server.get_portfolio_summary()
    _assert_json_serializable(combined_summary)
    assert combined_summary["owner"] == "Combined"
    assert combined_summary["totals"]["holdings_count"] >= 10
    assert combined_summary["totals"]["mf_count"] == 6
    assert combined_summary["totals"]["current_value"] > combined_summary["totals"]["invested"]

    ab_excl = mcp_server.get_portfolio_summary(
        owner="Abhishek",
        exclude_symbols=["RELIANCE", "TCS"],
        include_mf=False,
    )
    _assert_json_serializable(ab_excl)
    assert ab_excl["owner"] == "Abhishek"
    assert ab_excl["excluded_symbols"] == ["RELIANCE", "TCS"]
    assert ab_excl["totals"]["mf_count"] == 0
    assert ab_excl["totals"]["holdings_count"] == 5


def test_mcp_holdings_detail_and_mf_holdings_detail(seeded_mcp_db: Path, mock_market):
    tanu_eq = mcp_server.get_holdings_detail(owner="Tanu", exclude_symbols=["ITC"])
    _assert_json_serializable(tanu_eq)
    assert tanu_eq["owner"] == "Tanu"
    syms = {h["symbol"] for h in tanu_eq["holdings"]}
    assert "ITC" not in syms
    assert {"HDFCBANK", "SBIN", "INFY", "PIDILITIND"} == syms

    ab_mf = mcp_server.get_mf_holdings_detail(owner="Abhishek")
    _assert_json_serializable(ab_mf)
    assert ab_mf["owner"] == "Abhishek"
    assert ab_mf["count"] == 3
    assert ab_mf["totals"]["current_value"] > ab_mf["totals"]["invested"]
    assert abs(sum(m["weight_pct"] for m in ab_mf["mf_holdings"]) - 100.0) < 0.2

    # Verify cost fallback & JSON safety when NAV lookup returns None
    clear_market_cache()
    with patch("core.market_data._get_mftool", return_value=None):
        fallback_mf = mcp_server.get_mf_holdings_detail(owner="Tanu")
    _assert_json_serializable(fallback_mf)
    assert fallback_mf["count"] == 3
    assert all(m["price_missing"] is True for m in fallback_mf["mf_holdings"])
    assert fallback_mf["totals"]["current_value"] == fallback_mf["totals"]["invested"]


def test_mcp_transactions_history_filters(seeded_mcp_db: Path):
    rel_txs = mcp_server.get_transactions_history(
        owner="Abhishek",
        start_date="2024-01-01",
        end_date="2024-12-31",
        symbol="RELIANCE",
    )
    _assert_json_serializable(rel_txs)
    assert rel_txs["owner"] == "Abhishek"
    assert rel_txs["symbol"] == "RELIANCE"
    assert rel_txs["count"] == 3
    sides = [t["side"] for t in rel_txs["transactions"]]
    assert "BONUS" in sides

    split_txs = mcp_server.get_transactions_history(owner="Abhishek", symbol="PERSISTENT")
    _assert_json_serializable(split_txs)
    assert any(t["side"] == "SPLIT" for t in split_txs["transactions"])


def test_mcp_compare_against_benchmark(seeded_mcp_db: Path, mock_market):
    res = mcp_server.compare_against_benchmark(
        owner="Abhishek",
        benchmark="Nifty Midcap 150",
        start_date="2024-01-15",
        end_date="2024-06-30",
        exclude_symbols=["RELIANCE", "TCS"],
    )
    _assert_json_serializable(res)
    assert res["summary"]["owner"] == "Abhishek"
    assert res["summary"]["benchmark_key"] == "NIFTY_MIDCAP_150"
    assert res["summary"]["mode"] == "transactions"
    assert res["excluded_symbols"] == ["RELIANCE", "TCS"]
    assert "RELIANCE" not in res["included_symbols"]
    assert "TCS" not in res["included_symbols"]
    assert res["summary"]["portfolio_twr_pct"] is not None
    assert res["summary"]["benchmark_twr_pct"] is not None
    assert res["summary"]["alpha_twr_pct"] is not None
    assert len(res["timeseries"]) > 20
    assert res["timeseries"][0]["date"] >= "2024-01-15"
    assert res["timeseries"][-1]["date"] <= "2024-06-30"


def test_mcp_check_mf_stock_overlap(seeded_mcp_db: Path, mock_market):
    overlap = mcp_server.check_mf_stock_overlap(owner="Abhishek")
    _assert_json_serializable(overlap)
    assert overlap["summary"]["owner"] == "Abhishek"
    assert overlap["summary"]["overlapping_direct_stocks_count"] >= 5

    overlap_syms = {item["symbol"] for item in overlap["direct_vs_mf"]}
    assert {"HDFCBANK", "ICICIBANK", "RELIANCE", "PERSISTENT"}.issubset(overlap_syms)

    pairs = overlap["mf_overlap_matrix"]["pairs"]
    pp_vs_uti = next(
        p for p in pairs
        if {p["scheme_a"], p["scheme_b"]}
        == {
            "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
            "UTI Nifty 50 Index Fund - Direct Plan - Growth",
        }
    )
    assert pp_vs_uti["overlap_pct"] > 20.0
    assert "HDFCBANK" in pp_vs_uti["common_stocks"]


def test_mcp_add_trade_delta_and_symbol_mapping(seeded_mcp_db: Path, mock_market):
    delta_res = mcp_server.add_trade_delta(
        trades_text="2026-11-20 BUY WIPRO 10 @ 560\n2026-11-21 BUY WIPRO 5 @ 575",
        owner="Abhishek",
        broker="5paisa",
        rebuild_holdings=True,
    )
    _assert_json_serializable(delta_res)
    assert delta_res["parsed_count"] == 2
    assert delta_res["inserted"] == 2
    assert delta_res["rebuilt"]["upserted"] == 8

    # Re-entering same delta skips duplicates cleanly
    dup_res = mcp_server.add_trade_delta(
        trades_text="2026-11-20 BUY WIPRO 10 @ 560",
        owner="Abhishek",
    )
    _assert_json_serializable(dup_res)
    assert dup_res["inserted"] == 0
    assert dup_res["skipped"] == 1

    holdings = mcp_server.get_holdings_detail(owner="Abhishek")
    wipro = next(h for h in holdings["holdings"] if h["symbol"] == "WIPRO")
    assert wipro["quantity"] == 15.0

    # Symbol mapping tool
    map_res = mcp_server.add_symbol_mapping(
        raw_symbol="M&M",
        yahoo_symbol="M&M.NS",
        isin="INE101A01026",
        company_name="Mahindra & Mahindra Ltd",
        sector="Automobile",
    )
    _assert_json_serializable(map_res)
    assert map_res["status"] == "saved"
    assert resolve_yahoo_symbol("M&M", db_path=seeded_mcp_db) == "M&M.NS"
    aliases = get_symbol_aliases(db_path=seeded_mcp_db)
    assert aliases["M&M"]["sector"] == "Automobile"


def test_mcp_run_readonly_sql_allows_select_and_blocks_mutations(seeded_mcp_db: Path):
    res = mcp_server.run_readonly_sql(
        """
        SELECT a.owner, COUNT(h.id) AS stock_count, ROUND(SUM(h.invested_value), 2) AS total_invested
        FROM accounts a
        JOIN holdings h ON h.account_id = a.id
        GROUP BY a.owner
        ORDER BY a.owner
        """
    )
    _assert_json_serializable(res)
    assert res["row_count"] == 2
    assert [r["owner"] for r in res["rows"]] == ["Abhishek", "Tanu"]

    cte_res = mcp_server.run_readonly_sql(
        "WITH mf AS (SELECT account_id, COUNT(*) AS cnt FROM mf_holdings GROUP BY account_id) SELECT * FROM mf"
    )
    _assert_json_serializable(cte_res)
    assert cte_res["row_count"] == 2

    pragma_res = mcp_server.run_readonly_sql("PRAGMA table_info(holdings)")
    _assert_json_serializable(pragma_res)
    assert any(col["name"] == "extra_data" for col in pragma_res["rows"])

    # String literals containing SQL keywords in LIKE must NOT be false-positive blocked
    like_res = mcp_server.run_readonly_sql("SELECT symbol FROM holdings WHERE symbol LIKE '%UPDATE %'")
    _assert_json_serializable(like_res)
    assert like_res["row_count"] == 0

    # Connection context manager must actually close underlying sqlite3 connections
    from core.db import get_connection
    with get_connection(seeded_mcp_db) as closed_chk:
        closed_chk.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        closed_chk.execute("SELECT 1")

    bad_queries = [
        "DROP TABLE accounts",
        "DELETE FROM transactions WHERE id = 1",
        "INSERT INTO accounts (owner, broker, account_name, created_at) VALUES ('X', '5paisa', 'X', '2026-01-01')",
        "UPDATE holdings SET quantity = 0",
        "ALTER TABLE accounts ADD COLUMN foo TEXT",
        "CREATE TABLE evil(x INT)",
        "ATTACH DATABASE ':memory:' AS extra_db",
        "PRAGMA query_only = OFF",
        "SELECT 1; DROP TABLE accounts;",
    ]
    for bad_sql in bad_queries:
        with pytest.raises((ValueError, sqlite3.Error)):
            mcp_server.run_readonly_sql(bad_sql)


def test_gemini_chat_tool_loop_and_direct_tools(seeded_mcp_db: Path, mock_market):
    from types import SimpleNamespace
    from core.gemini_chat import ask_portfolio_gemini, build_portfolio_tools

    tools, tool_map = build_portfolio_tools(db_path=seeded_mcp_db)
    assert len(tools) == 7
    summary = tool_map["tool_get_portfolio_summary"](owner="Abhishek")
    _assert_json_serializable(summary)
    assert summary["owner"] == "Abhishek"

    # Mock google.genai.Client to test a 1-round function call -> final reply
    fc = SimpleNamespace(name="tool_get_portfolio_summary", args={"owner": "Abhishek"})
    first_resp = SimpleNamespace(
        function_calls=[fc],
        candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[]))],
        text="",
    )
    second_resp = SimpleNamespace(
        function_calls=[],
        candidates=[],
        text="Abhishek holds 7 direct stocks and 3 mutual funds.",
    )

    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, model, contents, config):
            self.calls += 1
            return first_resp if self.calls == 1 else second_resp

    class FakeClient:
        def __init__(self, api_key: str):
            self.models = FakeModels()

    with patch("google.genai.Client", FakeClient):
        out = ask_portfolio_gemini(
            prompt="Summarize Abhishek's portfolio",
            history=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}],
            api_key="fake-key",
            active_owner="Abhishek",
            db_path=seeded_mcp_db,
            enable_tools=True,
        )

    assert "Abhishek holds 7 direct stocks" in out["reply"]
    assert len(out["tools_used"]) == 1
    assert out["tools_used"][0]["tool"] == "tool_get_portfolio_summary"

    # Also verify fast single-shot raw streaming path
    from core.gemini_chat import stream_portfolio_gemini

    class FakeStreamModels:
        def generate_content_stream(self, model, contents, config):
            yield SimpleNamespace(text="RC Capital: ")
            yield SimpleNamespace(text="₹12.5 L vs Fintrekk: ₹15.0 L")

    class FakeStreamClient:
        def __init__(self, api_key: str):
            self.models = FakeStreamModels()

    with patch("google.genai.Client", FakeStreamClient):
        streamed = "".join(
            stream_portfolio_gemini(
                prompt="Compare RC Capital vs Fintrekk",
                api_key="fake-key",
                active_owner="Abhishek",
                db_path=seeded_mcp_db,
            )
        )
    assert "RC Capital: ₹12.5 L" in streamed

