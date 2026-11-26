"""Tests for core.market_data, core.analytics, and core.overlap."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.analytics import compare_portfolio_vs_benchmark, compute_xirr, get_portfolio_snapshot
from core.db import (
    get_or_create_account,
    init_db,
    insert_transactions,
    save_symbol_alias,
    upsert_holdings,
    upsert_mf_holdings,
)
from core.market_data import (
    clear_market_cache,
    fetch_benchmark_series,
    fetch_historical_prices,
    fetch_latest_prices,
    fetch_mf_nav,
    fetch_stock_splits,
    resolve_yahoo_symbol,
)
from core.overlap import analyze_mf_stock_overlap


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_market_cache()
    yield
    clear_market_cache()


def test_compute_xirr_known_annual_and_irregular():
    # Invest 1000 on 2024-01-01, value 1150 on 2025-01-01 (366 days in 2024 leap year)
    rate = compute_xirr([(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1150.0)])
    assert rate is not None
    assert abs(rate - 0.14956) < 1e-3

    # Multi-SIP positive return
    sip_rate = compute_xirr(
        [
            (date(2023, 1, 1), -10000.0),
            (date(2023, 7, 1), -10000.0),
            (date(2024, 1, 1), 22000.0),
        ]
    )
    assert sip_rate is not None
    assert 0.10 < sip_rate < 0.20

    # Negative return
    loss_rate = compute_xirr([(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 700.0)])
    assert loss_rate is not None
    assert abs(loss_rate - (-0.2993)) < 1e-3


def test_compute_xirr_invalid_cases():
    assert compute_xirr([]) is None
    assert compute_xirr([(date(2024, 1, 1), -1000.0)]) is None
    assert compute_xirr([(date(2024, 1, 1), -1000.0), (date(2024, 1, 1), 1200.0)]) is None
    assert compute_xirr([(date(2024, 1, 1), -1000.0), (date(2024, 6, 1), -500.0)]) is None


def test_resolve_yahoo_symbol_aliases_overrides_and_probe(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path, seed_default_owners=False)
    save_symbol_alias("CUSTOMSCRIP", "CUSTOMSCRIP.BO", isin="INE001A01001", db_path=db_path)

    assert resolve_yahoo_symbol("CUSTOMSCRIP", db_path=db_path) == "CUSTOMSCRIP.BO"
    assert resolve_yahoo_symbol("RELIANCE", db_path=db_path) == "RELIANCE.NS"

    # Probe .NS failing then .BO succeeding
    def fake_ticker(sym):
        mock_t = MagicMock()
        if sym == "BSETEST.NS":
            mock_t.history.return_value = pd.DataFrame()
        elif sym == "BSETEST.BO":
            mock_t.history.return_value = pd.DataFrame(
                {"Close": [123.45]},
                index=pd.to_datetime(["2025-01-02"]),
            )
        return mock_t

    with patch("core.market_data.yf.Ticker", side_effect=fake_ticker):
        resolved = resolve_yahoo_symbol("BSETEST", db_path=db_path)
    assert resolved == "BSETEST.BO"


def test_fetch_latest_prices_and_missing_ticker(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path, seed_default_owners=False)

    idx = pd.to_datetime(["2025-01-01", "2025-01-02"])
    cols = pd.MultiIndex.from_tuples([("Close", "RELIANCE.NS"), ("Close", "DELISTED.NS")])
    mock_df = pd.DataFrame([[1200.0, float("nan")], [1260.0, float("nan")]], index=idx, columns=cols)

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()

    with (
        patch("core.market_data.resolve_yahoo_symbol", side_effect=lambda s, **kw: f"{s}.NS"),
        patch("core.market_data.yf.download", return_value=mock_df),
        patch("core.market_data.yf.Ticker", return_value=mock_ticker),
    ):
        res = fetch_latest_prices(["RELIANCE", "DELISTED"], db_path=db_path)

    assert res["RELIANCE"]["price"] == 1260.0
    assert res["RELIANCE"]["prev_close"] == 1200.0
    assert res["RELIANCE"]["change_pct"] == 5.0
    assert res["RELIANCE"]["currency"] == "INR"
    assert res["DELISTED"]["price"] is None


def test_fetch_historical_prices_and_benchmark_fallback(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path, seed_default_owners=False)

    idx = pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"])
    cols = pd.MultiIndex.from_tuples([("Close", "TCS.NS"), ("Close", "INFY.NS")])
    mock_df = pd.DataFrame(
        [[4000.0, 1800.0], [4050.0, 1820.0], [4100.0, 1850.0]],
        index=idx,
        columns=cols,
    )

    with (
        patch("core.market_data.resolve_yahoo_symbol", side_effect=lambda s, **kw: f"{s}.NS"),
        patch("core.market_data.yf.download", return_value=mock_df) as dl_mock,
    ):
        hist = fetch_historical_prices(["TCS", "INFY"], "2025-01-01", "2025-01-03", db_path=db_path)

    assert list(hist.columns) == ["TCS", "INFY"]
    assert len(hist) == 3
    assert hist.loc[pd.Timestamp("2025-01-03"), "TCS"] == 4100.0
    assert dl_mock.call_args.kwargs["auto_adjust"] is True

    # Benchmark fallback: first ticker empty, second succeeds
    b_idx = pd.to_datetime(["2025-01-01", "2025-01-02"])
    b_df = pd.DataFrame(
        [[200.0], [210.0]],
        index=b_idx,
        columns=pd.MultiIndex.from_tuples([("Close", "MIDCAPETF.NS")]),
    )

    def bench_dl(tickers, **kwargs):
        if tickers == "NIFTYMIDCAP150.NS":
            return pd.DataFrame()
        return b_df

    mock_empty_ticker = MagicMock()
    mock_empty_ticker.history.return_value = pd.DataFrame()

    with (
        patch("core.market_data.yf.download", side_effect=bench_dl),
        patch("core.market_data.yf.Ticker", return_value=mock_empty_ticker),
    ):
        bench = fetch_benchmark_series("NIFTY_MIDCAP_150", "2025-01-01", "2025-01-02")

    assert len(bench) == 2
    assert bench.iloc[-1] == 210.0


def test_fetch_mf_nav_and_stock_splits(tmp_path: Path):
    fake_mf = MagicMock()
    fake_mf.get_scheme_codes.return_value = {
        "122639": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "122640": "Parag Parikh Flexi Cap Fund - Regular Plan - Growth",
    }
    fake_mf.get_scheme_quote.return_value = {
        "scheme_code": "122639",
        "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "plan": "Direct",
        "option": "Growth",
        "nav": "85.42",
        "last_updated": "24-Nov-2026",
    }

    with patch("core.market_data._get_mftool", return_value=fake_mf):
        nav = fetch_mf_nav(scheme_name="Parag Parikh Flexi Cap Fund Direct Growth")
    assert nav is not None
    assert nav["scheme_code"] == "122639"
    assert nav["nav"] == 85.42

    # Stock splits with tz-aware index
    splits_series = pd.Series(
        [5.0, 2.0],
        index=pd.to_datetime(["2022-06-01", "2024-03-15"]).tz_localize("Asia/Kolkata"),
    )
    mock_t = MagicMock()
    mock_t.splits = splits_series
    with (
        patch("core.market_data.resolve_yahoo_symbol", return_value="TEST.NS"),
        patch("core.market_data.yf.Ticker", return_value=mock_t),
    ):
        s = fetch_stock_splits("TEST", start_date="2023-01-01", db_path=tmp_path / "x.db")
    assert len(s) == 1
    assert s.iloc[0] == 2.0


def test_portfolio_snapshot_owner_filter_and_exclusions(tmp_path: Path):
    db_path = tmp_path / "port.db"
    init_db(db_path, seed_default_owners=False)
    abhi = get_or_create_account("Abhishek", "5paisa", db_path=db_path)
    tanu = get_or_create_account("Tanu", "5paisa", db_path=db_path)

    upsert_holdings(
        abhi["id"],
        [
            {"symbol": "RELIANCE", "quantity": 10, "avg_price": 1000.0, "invested_value": 10000.0},
            {"symbol": "TCS", "quantity": 5, "avg_price": 3000.0, "invested_value": 15000.0},
        ],
        db_path=db_path,
    )
    upsert_holdings(
        tanu["id"],
        [{"symbol": "INFY", "quantity": 10, "avg_price": 1500.0, "invested_value": 15000.0}],
        db_path=db_path,
    )
    upsert_mf_holdings(
        abhi["id"],
        [
            {
                "folio_number": "F1",
                "scheme_name": "Parag Parikh Flexi Cap Fund Direct Growth",
                "amfi_code": "122639",
                "units": 100.0,
                "avg_nav": 50.0,
                "invested_value": 5000.0,
            }
        ],
        db_path=db_path,
    )

    mock_prices = {
        "RELIANCE": {"price": 1200.0, "prev_close": 1150.0, "change_pct": 4.3478, "yahoo_symbol": "RELIANCE.NS", "currency": "INR"},
        "TCS": {"price": 3400.0, "prev_close": 3350.0, "change_pct": 1.4925, "yahoo_symbol": "TCS.NS", "currency": "INR"},
        "INFY": {"price": 1600.0, "prev_close": 1600.0, "change_pct": 0.0, "yahoo_symbol": "INFY.NS", "currency": "INR"},
    }
    mock_nav = {
        "scheme_code": "122639",
        "scheme_name": "Parag Parikh Flexi Cap Fund Direct Growth",
        "nav": 80.0,
        "last_updated": "24-Nov-2026",
        "plan": "Direct",
        "option": "Growth",
    }

    with (
        patch("core.analytics.fetch_latest_prices", side_effect=lambda syms, **kw: {s: mock_prices[s] for s in syms}),
        patch("core.analytics.fetch_mf_nav", return_value=mock_nav),
    ):
        abhi_snap = get_portfolio_snapshot(owner="Abhishek", exclude_symbols=["TCS"], include_mf=True, db_path=db_path)
        combined_snap = get_portfolio_snapshot(owner=None, include_mf=False, db_path=db_path)

    # Abhishek excluding TCS: RELIANCE (12,000 on 10,000) + MF (8,000 on 5,000)
    assert abhi_snap["totals"]["invested"] == 15000.0
    assert abhi_snap["totals"]["current_value"] == 20000.0
    assert abhi_snap["totals"]["pnl"] == 5000.0
    assert abhi_snap["totals"]["holdings_count"] == 1
    assert abhi_snap["totals"]["mf_count"] == 1
    assert sum(h["weight_pct"] for h in abhi_snap["holdings"]) == pytest.approx(100.0, abs=0.05)

    # Combined equity only: RELIANCE (12,000) + TCS (17,000) + INFY (16,000) = 45,000
    assert combined_snap["totals"]["current_value"] == 45000.0
    assert combined_snap["totals"]["holdings_count"] == 3


def test_compare_portfolio_vs_benchmark_splits_and_fallback(tmp_path: Path):
    db_path = tmp_path / "bench.db"
    init_db(db_path, seed_default_owners=False)
    abhi = get_or_create_account("Abhishek", "5paisa", db_path=db_path)

    # Bought 10 shares @ 1000 on 2024-01-02; stock had a 1:5 split on 2024-02-01.
    # Yahoo auto_adjust=True price is 200 on 2024-01-02 and 240 on 2024-03-01 (+20%).
    insert_transactions(
        abhi["id"],
        [
            {
                "trade_date": "2024-01-02",
                "symbol": "SPLITCO",
                "side": "BUY",
                "quantity": 10.0,
                "price": 1000.0,
                "amount": 10000.0,
            }
        ],
        db_path=db_path,
    )

    dates = pd.to_datetime(["2024-01-02", "2024-02-01", "2024-03-01"])
    hist_df = pd.DataFrame({"SPLITCO": [200.0, 220.0, 240.0]}, index=dates)
    bench_s = pd.Series([100.0, 105.0, 110.0], index=dates, name="NIFTY_MIDCAP_150")
    splits_s = pd.Series([5.0], index=pd.to_datetime(["2024-02-01"]))

    with (
        patch("core.analytics.fetch_stock_splits", return_value=splits_s),
        patch("core.analytics.fetch_historical_prices", return_value=hist_df),
        patch("core.analytics.fetch_benchmark_series", return_value=bench_s),
    ):
        res = compare_portfolio_vs_benchmark(
            owner="Abhishek",
            benchmark_key="NIFTY_MIDCAP_150",
            start_date="2024-01-02",
            end_date="2024-03-01",
            adjust_for_splits=True,
            db_path=db_path,
        )

    summary = res["summary"]
    assert summary["mode"] == "transactions"
    assert summary["portfolio_end_value"] == 12000.0  # 10 * 5 * 240 = 12,000 (+20% on 10,000)
    assert summary["benchmark_matched_value"] == 11000.0  # +10% on 10,000
    assert summary["portfolio_twr_pct"] == 20.0
    assert summary["benchmark_twr_pct"] == 10.0
    assert summary["alpha_twr_pct"] == 10.0

    # Holdings fallback mode when no transactions exist for Tanu
    tanu = get_or_create_account("Tanu", "5paisa", db_path=db_path)
    upsert_holdings(
        tanu["id"],
        [{"symbol": "SPLITCO", "quantity": 50.0, "avg_price": 200.0, "invested_value": 10000.0}],
        db_path=db_path,
    )
    with (
        patch("core.analytics.fetch_historical_prices", return_value=hist_df),
        patch("core.analytics.fetch_benchmark_series", return_value=bench_s),
    ):
        fb = compare_portfolio_vs_benchmark(
            owner="Tanu",
            benchmark_key="NIFTY_MIDCAP_150",
            start_date="2024-01-02",
            end_date="2024-03-01",
            db_path=db_path,
        )
    assert fb["summary"]["mode"] == "holdings_fallback"
    assert fb["summary"]["portfolio_start_value"] == 10000.0
    assert fb["summary"]["portfolio_end_value"] == 12000.0


def test_analyze_mf_stock_overlap(tmp_path: Path):
    db_path = tmp_path / "overlap.db"
    init_db(db_path, seed_default_owners=False)
    abhi = get_or_create_account("Abhishek", "5paisa", db_path=db_path)

    upsert_holdings(
        abhi["id"],
        [
            {"symbol": "HDFCBANK", "quantity": 10, "avg_price": 1500.0, "invested_value": 15000.0},
            {"symbol": "INFY", "quantity": 10, "avg_price": 1500.0, "invested_value": 15000.0},
        ],
        db_path=db_path,
    )
    upsert_mf_holdings(
        abhi["id"],
        [
            {
                "folio_number": "1",
                "scheme_name": "UTI Nifty 50 Index Fund - Direct Growth",
                "amfi_code": "120716",
                "units": 100.0,
                "avg_nav": 100.0,
                "invested_value": 10000.0,
            },
            {
                "folio_number": "2",
                "scheme_name": "Custom Active Flexi Cap Fund",
                "amfi_code": "999001",
                "units": 100.0,
                "avg_nav": 100.0,
                "invested_value": 10000.0,
                "extra_data": {"constituents": {"HDFCBANK": 8.0, "ICICIBANK": 7.0}},
            },
            {
                "folio_number": "3",
                "scheme_name": "Unmapped Mid Cap Fund - Direct Growth",
                "amfi_code": "999002",
                "units": 100.0,
                "avg_nav": 100.0,
                "invested_value": 10000.0,
            },
        ],
        db_path=db_path,
    )

    mock_prices = {
        "HDFCBANK": {"price": 1600.0, "prev_close": 1600.0, "change_pct": 0.0, "yahoo_symbol": "HDFCBANK.NS", "currency": "INR"},
        "INFY": {"price": 1600.0, "prev_close": 1600.0, "change_pct": 0.0, "yahoo_symbol": "INFY.NS", "currency": "INR"},
    }

    with (
        patch("core.analytics.fetch_latest_prices", return_value=mock_prices),
        patch("core.analytics.fetch_mf_nav", return_value=None),  # value MFs at 10,000 cost
        patch("core.overlap._fetch_scheme_metadata", return_value={"scheme_category": "Mid Cap Fund"}),
        patch(
            "core.overlap._fetch_nse_index_constituents",
            return_value={"HDFCBANK": 12.0, "RELIANCE": 10.0, "INFY": 6.0},
        ),
    ):
        ov = analyze_mf_stock_overlap(owner="Abhishek", db_path=db_path)

    assert ov["summary"]["overlapping_direct_stocks_count"] == 2
    assert ov["summary"]["funds_with_constituents"] == 2
    assert ov["summary"]["funds_without_constituents"] == 1

    # HDFCBANK direct = 16,000; MF implied = 12% of 10,000 (1,200) + 8% of 10,000 (800) = 2,000
    hdfc_row = next(r for r in ov["direct_vs_mf"] if r["symbol"] == "HDFCBANK")
    assert hdfc_row["direct_value"] == 16000.0
    assert hdfc_row["mf_implied_value"] == 2000.0
    assert hdfc_row["total_effective_value"] == 18000.0

    # Matrix: UTI Nifty 50 vs Custom Active shares HDFCBANK min(12.0, 8.0) = 8.0%
    mat = ov["mf_overlap_matrix"]["matrix"]
    assert mat["UTI Nifty 50 Index Fund - Direct Growth"]["Custom Active Flexi Cap Fund"] == 8.0
    # Unmapped fund must be None (not fabricated!)
    assert mat["UTI Nifty 50 Index Fund - Direct Growth"]["Unmapped Mid Cap Fund - Direct Growth"] is None


def test_compare_mf_vs_benchmark(tmp_path: Path):
    from core.analytics import compare_mf_vs_benchmark

    db_path = tmp_path / "mf_bench.db"
    init_db(db_path=db_path, seed_default_owners=False)
    acc = get_or_create_account("Abhishek", broker="CDSL", db_path=db_path)
    upsert_mf_holdings(
        account_id=acc["id"],
        mf_holdings=[
            {
                "folio_number": "F1",
                "scheme_name": "Capitalmind Flexi Cap Fund",
                "amfi_code": "153738",
                "units": 1000.0,
                "avg_nav": 10.0,
                "invested_value": 10000.0,
                "as_of_date": "2026-11-25",
            },
            {
                "folio_number": "F2",
                "scheme_name": "ICICI Prudential NASDAQ 100 Index Fund",
                "amfi_code": "149219",
                "units": 100.0,
                "avg_nav": 20.0,
                "invested_value": 2000.0,
                "as_of_date": "2026-11-25",
            },
        ],
        db_path=db_path,
    )

    dates = pd.to_datetime(["2026-11-10", "2026-11-17", "2026-11-24"])
    nav_153738 = pd.Series([10.0, 10.5, 11.0], index=dates)
    nav_149219 = pd.Series([20.0, 21.0, 22.0], index=dates)
    bench_s = pd.Series([1000.0, 1020.0, 1050.0], index=dates)

    def _fake_mf_hist(amfi_code="", scheme_name="", start_date=None, end_date=None):
        return nav_153738 if amfi_code == "153738" else nav_149219

    with (
        patch("core.analytics.fetch_mf_historical_nav", side_effect=_fake_mf_hist),
        patch("core.analytics.fetch_benchmark_series", return_value=bench_s),
    ):
        all_res = compare_mf_vs_benchmark(
            owner="Abhishek",
            benchmark_key="NIFTY_50",
            start_date="2026-11-10",
            end_date="2026-11-24",
            scheme_filter="ALL",
            db_path=db_path,
        )
        single_res = compare_mf_vs_benchmark(
            owner="Abhishek",
            benchmark_key="NIFTY_50",
            start_date="2026-11-10",
            end_date="2026-11-24",
            scheme_filter="153738",
            db_path=db_path,
        )

    assert all_res["summary"]["portfolio_start_value"] == pytest.approx(12000.0)
    assert all_res["summary"]["portfolio_end_value"] == pytest.approx(13200.0)
    assert all_res["summary"]["portfolio_twr_pct"] == pytest.approx(10.0)
    assert all_res["summary"]["benchmark_twr_pct"] == pytest.approx(5.0)
    assert all_res["summary"]["alpha_twr_pct"] == pytest.approx(5.0)
    assert single_res["summary"]["portfolio_end_value"] == pytest.approx(11000.0)
    assert len(single_res["included_symbols"]) == 1


def test_cap_distribution_and_gemini_refresh(tmp_path: Path):
    """Verify cap_allocations DB cache, value-weighted cap math across segments, and Gemini JSON import."""
    import json
    from types import SimpleNamespace
    from core.analytics import compute_cap_distribution
    from core.db import init_db, query_cap_allocations
    from core.gemini_chat import fetch_cap_allocations_via_gemini

    db_path = tmp_path / "cap.db"
    init_db(db_path, seed_default_owners=False)

    holdings = [
        {"asset_class": "EQUITY", "symbol": "RELIANCE", "company_name": "Reliance Industries", "region": "India", "advisory_source": "RC Capital", "current_value": 60000.0},
        {"asset_class": "EQUITY", "symbol": "SMALLCO", "company_name": "Small Cap Co", "region": "India", "advisory_source": "Fintrekk Capital", "current_value": 40000.0},
        {"asset_class": "MF", "scheme_name": "Flexi Cap Fund", "region": "India", "advisory_source": "Self", "current_value": 100000.0},
        {"asset_class": "EQUITY", "symbol": "LIQUIDETF", "company_name": "Liquid ETF", "region": "India", "advisory_source": "Self", "current_value": 100000.0},
        {"asset_class": "EQUITY", "symbol": "MON100", "company_name": "MOMF NASDAQ 100 ETF", "region": "Foreign", "advisory_source": "Self", "current_value": 500000.0},
        {"asset_class": "MF", "scheme_name": "Parag Parikh IFSC NASDAQ 100 FoF", "region": "Foreign", "advisory_source": "Self", "current_value": 200000.0},
    ]

    fake_payload = {
        "RELIANCE": {"large": 100, "mid": 0, "small": 0, "other": 0},
        "SMALLCO": {"large": 0, "mid": 0, "small": 100, "other": 0},
        "FLEXI CAP FUND": {"large": 60, "mid": 30, "small": 10, "other": 0},
        "LIQUIDETF": {"large": 0, "mid": 0, "small": 0, "other": 100},
    }

    class FakeModels:
        def generate_content(self, model, contents, config):
            return SimpleNamespace(text=json.dumps(fake_payload))

    class FakeClient:
        def __init__(self, api_key: str):
            self.models = FakeModels()

    with patch("google.genai.Client", FakeClient):
        res = fetch_cap_allocations_via_gemini(
            holdings=holdings,
            api_key="fake-key",
            db_path=db_path,
        )

    assert res["saved"] == 4
    cap_map = query_cap_allocations(db_path=db_path)
    assert len(cap_map) == 4

    all_dist = compute_cap_distribution(holdings, cap_map, segment="All Combined")
    # Total = 1,000,000: Large = 120k (12%), Mid = 30k (3%), Small = 50k (5%), Cash = 100k (10%), Foreign = 700k (70%)
    assert all_dist["large_pct"] == pytest.approx(12.0)
    assert all_dist["mid_pct"] == pytest.approx(3.0)
    assert all_dist["small_pct"] == pytest.approx(5.0)
    assert all_dist["cash_pct"] == pytest.approx(10.0)
    assert all_dist["foreign_pct"] == pytest.approx(70.0)
    assert all_dist["coverage_pct"] == pytest.approx(100.0)
    assert all_dist["mapped_count"] == 6

    eq_dist = compute_cap_distribution(holdings, cap_map, segment="Equities Only")
    # Equity Total = 700,000: RELIANCE 60k Large, SMALLCO 40k Small, LIQUIDETF 100k Cash, MON100 500k Foreign
    assert eq_dist["large_pct"] == pytest.approx(60000 / 700000 * 100, abs=0.05)
    assert eq_dist["small_pct"] == pytest.approx(40000 / 700000 * 100, abs=0.05)
    assert eq_dist["cash_pct"] == pytest.approx(100000 / 700000 * 100, abs=0.05)
    assert eq_dist["foreign_pct"] == pytest.approx(500000 / 700000 * 100, abs=0.05)

    mf_dist = compute_cap_distribution(holdings, cap_map, segment="Mutual Funds Only")
    # MF Total = 300,000: Flexi (60k L, 30k M, 10k S), PPFAS GIFT 200k Foreign
    assert mf_dist["large_pct"] == pytest.approx(20.0)
    assert mf_dist["mid_pct"] == pytest.approx(10.0)
    assert mf_dist["small_pct"] == pytest.approx(3.33, abs=0.05)
    assert mf_dist["foreign_pct"] == pytest.approx(66.67, abs=0.05)

    rc_dist = compute_cap_distribution(holdings, cap_map, segment="RC Capital")
    assert rc_dist["large_pct"] == pytest.approx(100.0)
    assert rc_dist["total_count"] == 1


def test_mf_usd_conversion_and_usd_xirr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify USD-denominated MF holdings & transactions convert cleanly to INR in snapshot and XIRR."""
    import core.analytics as analytics
    from core.db import get_or_create_account, init_db, insert_transactions, upsert_mf_holdings

    db_path = tmp_path / "mf_usd.db"
    init_db(db_path, seed_default_owners=False)
    acc = get_or_create_account("Abhishek", broker="PPFAS IFSC", account_name="PPFAS GIFT", db_path=db_path)

    upsert_mf_holdings(
        acc["id"],
        [
            {
                "folio_number": "10004193",
                "scheme_name": "Parag Parikh IFSC NASDAQ 100 FoF",
                "amfi_code": "PPFAS_GIFT",
                "units": 100.0,
                "avg_nav": 100.0,
                "invested_value": 10000.0,
                "as_of_date": "2026-11-26",
                "region": "Foreign",
                "extra_data": {"cost_currency": "USD", "live_currency": "USD", "manual_ltp": 110.0},
            }
        ],
        db_path=db_path,
    )
    insert_transactions(
        acc["id"],
        [
            {
                "trade_date": "2025-11-26",
                "symbol": "PPFAS_GIFT",
                "exchange": "IFSC",
                "asset_type": "MF",
                "side": "BUY",
                "quantity": 100.0,
                "price": 100.0,
                "amount": 10000.0,
                "extra_data": {"currency": "USD"},
            }
        ],
        db_path=db_path,
    )

    monkeypatch.setattr(analytics, "fetch_usdinr_rate", lambda: 90.0)
    snap = analytics.get_portfolio_snapshot(owner="Abhishek", db_path=db_path)
    mf = snap["mf_holdings"][0]
    assert mf["invested_value"] == pytest.approx(900000.0)
    assert mf["current_value"] == pytest.approx(990000.0)
    assert mf["unrealized_pnl_pct"] == pytest.approx(10.0)
    # 1 year hold from 2025-11-26 to 2026-11-26 (+10% gain in both USD & INR) -> XIRR ~ 10%
    assert snap["totals"]["xirr"] == pytest.approx(0.10, abs=0.01)


def test_single_flight_warmup_ttl_failure_and_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify warmup single-flight sharing, TTL expiration, failure cleanup, and MF key alignment."""
    import threading
    import time
    import ui.data_service as ds

    db_path = tmp_path / "warm.db"
    db_path.write_bytes(b"")  # exists check

    with ds._BG_LOCK:
        ds._BG_BENCH_RESULTS.clear()
        ds._BG_INFLIGHT_FUTURES.clear()

    # 1. Verify Equity & MF warmup share NIFTY_MIDCAP_150 (matching tab_benchmark defaults)
    specs = ds.default_warmup_specs(owner="Abhishek", db_path=db_path, version=1)
    assert [s[0][0] for s in specs] == ["eq", "eq", "mf"]
    assert all(s[0][2] == "NIFTY_MIDCAP_150" for s in specs)

    # 2. Verify concurrent callers join the single in-flight Future instead of duplicating work
    call_count = 0
    started = threading.Event()
    proceed = threading.Event()

    def slow_compute():
        nonlocal call_count
        call_count += 1
        started.set()
        proceed.wait(timeout=2.0)
        return {"summary": {"ok": True}, "calls": call_count}

    key = ("eq", "Abhishek", "NIFTY_MIDCAP_150", "2025-11-26", "2026-11-26", (), True, str(db_path), 1, None, None)
    ds._run_or_join_single_flight(key, slow_compute, wait=False)
    assert started.wait(timeout=1.0)
    proceed.set()
    res = ds._run_or_join_single_flight(key, slow_compute, wait=True)
    assert call_count == 1
    assert res == {"summary": {"ok": True}, "calls": 1}

    # 3. Verify TTL expiration clears stale entries cleanly
    with ds._BG_LOCK:
        ds._BG_BENCH_RESULTS[key] = (time.monotonic() - (ds._BG_TTL_SECONDS + 5), {"stale": True})
    res_fresh = ds._run_or_join_single_flight(key, lambda: {"stale": False}, wait=True)
    assert res_fresh == {"stale": False}

    # 4. Verify failures remove key from _BG_INFLIGHT_FUTURES so retries succeed
    fail_key = ("mf", "Abhishek", "NIFTY_MIDCAP_150", "2025-11-26", "2026-11-26", "ALL", str(db_path), 1)
    with pytest.raises(RuntimeError):
        ds._run_or_join_single_flight(fail_key, lambda: (_ for _ in ()).throw(RuntimeError("boom")), wait=True)
    with ds._BG_LOCK:
        assert fail_key not in ds._BG_INFLIGHT_FUTURES
    assert ds._run_or_join_single_flight(fail_key, lambda: {"recovered": True}, wait=True) == {"recovered": True}


def test_gemini_stream_midstream_error_and_context_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify stream_portfolio_gemini never duplicates output after partial stream and context errors surface loudly."""
    from types import SimpleNamespace
    import core.gemini_chat as gc

    # 1. Context build failure must raise loudly instead of sending '{}' silently
    monkeypatch.setattr(gc, "build_local_portfolio_context", lambda **_kw: (_ for _ in ()).throw(ValueError("db corrupt")))
    with pytest.raises(RuntimeError, match="Failed to load local portfolio context"):
        list(gc.stream_portfolio_gemini(prompt="hi", api_key="fake-key", db_path=tmp_path / "x.db"))

    # Restore valid context and inspect prompt factsheet guardrail
    monkeypatch.setattr(gc, "build_local_portfolio_context", lambda **_kw: '{"holdings":[]}')
    sys_inst, _ = gc._prepare_gemini_request(
        prompt="overlap?",
        history=None,
        active_owner="All",
        db_path=tmp_path / "x.db",
        precomputed_snapshot=None,
    )
    assert "factsheet constituents are NOT in RAW_PORTFOLIO_DATA" in sys_inst

    # 2. Pre-stream 503 should fall back cleanly to next model
    class FallbackModels:
        def __init__(self):
            self.calls = []

        def generate_content_stream(self, model, contents, config):
            self.calls.append(model)
            if len(self.calls) == 1:
                raise RuntimeError("503 UNAVAILABLE")
            yield SimpleNamespace(text="clean-fallback")

    class PreFailClient:
        def __init__(self, api_key: str):
            self.models = FallbackModels()

    monkeypatch.setattr("google.genai.Client", PreFailClient)
    out = list(gc.stream_portfolio_gemini(prompt="hi", api_key="fake-key", model="gemini-3.6-flash", db_path=tmp_path / "x.db"))
    assert out == ["clean-fallback"]

    # 3. Mid-stream 503 after yielding a chunk must NOT restart on fallback model
    class MidFailModels:
        def __init__(self):
            self.calls = []

        def generate_content_stream(self, model, contents, config):
            self.calls.append(model)
            yield SimpleNamespace(text="partial-")
            raise RuntimeError("503 UNAVAILABLE mid-stream")

    mid_models = MidFailModels()

    class MidFailClient:
        def __init__(self, api_key: str):
            self.models = mid_models

    monkeypatch.setattr("google.genai.Client", MidFailClient)
    streamed: list[str] = []
    with pytest.raises(RuntimeError, match="mid-stream"):
        for chunk in gc.stream_portfolio_gemini(prompt="hi", api_key="fake-key", model="gemini-3.6-flash", db_path=tmp_path / "x.db"):
            streamed.append(chunk)
    assert streamed == ["partial-"]
    assert len(mid_models.calls) == 1
