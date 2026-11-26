"""Seed realistic sample Indian equity and mutual fund data for Abhishek and Tanu."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure project root is importable when executed directly as `python scripts/seed_demo.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import DB_PATH  # noqa: E402
from core.db import (  # noqa: E402
    get_connection,
    get_or_create_account,
    init_db,
    insert_transactions,
    rebuild_holdings_from_transactions,
    save_symbol_alias,
    upsert_mf_holdings,
)

_DEMO_SYMBOLS: list[dict[str, str]] = [
    {
        "raw_symbol": "RELIANCE",
        "yahoo_symbol": "RELIANCE.NS",
        "isin": "INE002A01018",
        "company_name": "Reliance Industries Ltd",
        "sector": "Energy & Conglomerate",
    },
    {
        "raw_symbol": "TCS",
        "yahoo_symbol": "TCS.NS",
        "isin": "INE467B01029",
        "company_name": "Tata Consultancy Services Ltd",
        "sector": "Information Technology",
    },
    {
        "raw_symbol": "TATAMOTORS",
        "yahoo_symbol": "TATAMOTORS.NS",
        "isin": "INE155A01022",
        "company_name": "Tata Motors Ltd",
        "sector": "Automobile",
    },
    {
        "raw_symbol": "HDFCBANK",
        "yahoo_symbol": "HDFCBANK.NS",
        "isin": "INE040A01034",
        "company_name": "HDFC Bank Ltd",
        "sector": "Banking & Finance",
    },
    {
        "raw_symbol": "INFY",
        "yahoo_symbol": "INFY.NS",
        "isin": "INE009A01021",
        "company_name": "Infosys Ltd",
        "sector": "Information Technology",
    },
    {
        "raw_symbol": "PERSISTENT",
        "yahoo_symbol": "PERSISTENT.NS",
        "isin": "INE262H01021",
        "company_name": "Persistent Systems Ltd",
        "sector": "Information Technology",
    },
    {
        "raw_symbol": "ICICIBANK",
        "yahoo_symbol": "ICICIBANK.NS",
        "isin": "INE090A01021",
        "company_name": "ICICI Bank Ltd",
        "sector": "Banking & Finance",
    },
    {
        "raw_symbol": "ITC",
        "yahoo_symbol": "ITC.NS",
        "isin": "INE154A01025",
        "company_name": "ITC Ltd",
        "sector": "Consumer Staples",
    },
    {
        "raw_symbol": "SBIN",
        "yahoo_symbol": "SBIN.NS",
        "isin": "INE062A01020",
        "company_name": "State Bank of India",
        "sector": "Banking & Finance",
    },
    {
        "raw_symbol": "PIDILITIND",
        "yahoo_symbol": "PIDILITIND.NS",
        "isin": "INE318A01026",
        "company_name": "Pidilite Industries Ltd",
        "sector": "Chemicals",
    },
]

_ABHISHEK_TRANSACTIONS: list[dict[str, Any]] = [
    {
        "trade_date": "2024-01-15",
        "symbol": "RELIANCE",
        "isin": "INE002A01018",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 20,
        "price": 2740.0,
        "fees": 54.8,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-07-10",
        "symbol": "RELIANCE",
        "isin": "INE002A01018",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 10,
        "price": 3120.0,
        "fees": 31.2,
        "source": "demo_seed",
    },
    {
        # 1:1 Bonus issue example: adds 30 free shares, halving average cost
        "trade_date": "2024-10-28",
        "symbol": "RELIANCE",
        "isin": "INE002A01018",
        "exchange": "NSE",
        "side": "BONUS",
        "quantity": 30,
        "price": 0.0,
        "amount": 0.0,
        "source": "demo_seed",
        "extra_data": {"corporate_action": "1:1 Bonus"},
    },
    {
        "trade_date": "2024-01-22",
        "symbol": "PERSISTENT",
        "isin": "INE262H01021",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 10,
        "price": 7900.0,
        "fees": 79.0,
        "source": "demo_seed",
    },
    {
        # 2:1 Stock split example: +10 shares (10 -> 20 shares)
        "trade_date": "2024-04-01",
        "symbol": "PERSISTENT",
        "isin": "INE262H01021",
        "exchange": "NSE",
        "side": "SPLIT",
        "quantity": 10,
        "price": 0.0,
        "amount": 0.0,
        "source": "demo_seed",
        "extra_data": {"corporate_action": "2:1 Stock Split"},
    },
    {
        "trade_date": "2024-02-05",
        "symbol": "TCS",
        "isin": "INE467B01029",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 15,
        "price": 3850.0,
        "fees": 57.75,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-11-20",
        "symbol": "TCS",
        "isin": "INE467B01029",
        "exchange": "NSE",
        "side": "SELL",
        "quantity": 5,
        "price": 4140.0,
        "fees": 25.0,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-03-12",
        "symbol": "TATAMOTORS",
        "isin": "INE155A01022",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 40,
        "price": 980.0,
        "fees": 39.2,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-04-08",
        "symbol": "HDFCBANK",
        "isin": "INE040A01034",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 30,
        "price": 1520.0,
        "fees": 45.6,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-05-14",
        "symbol": "INFY",
        "isin": "INE009A01021",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 25,
        "price": 1440.0,
        "fees": 36.0,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-06-18",
        "symbol": "ICICIBANK",
        "isin": "INE090A01021",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 35,
        "price": 1120.0,
        "fees": 39.2,
        "source": "demo_seed",
    },
    {
        "trade_date": "2025-02-15",
        "symbol": "TCS",
        "isin": "INE467B01029",
        "exchange": "NSE",
        "side": "DIVIDEND",
        "quantity": 10,
        "price": 60.0,
        "amount": 600.0,
        "source": "demo_seed",
    },
]

_TANU_TRANSACTIONS: list[dict[str, Any]] = [
    {
        "trade_date": "2024-02-12",
        "symbol": "ITC",
        "isin": "INE154A01025",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 100,
        "price": 415.0,
        "fees": 41.5,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-03-04",
        "symbol": "HDFCBANK",
        "isin": "INE040A01034",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 20,
        "price": 1460.0,
        "fees": 29.2,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-04-15",
        "symbol": "SBIN",
        "isin": "INE062A01020",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 50,
        "price": 755.0,
        "fees": 37.75,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-05-20",
        "symbol": "INFY",
        "isin": "INE009A01021",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 20,
        "price": 1455.0,
        "fees": 29.1,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-07-02",
        "symbol": "PIDILITIND",
        "isin": "INE318A01026",
        "exchange": "NSE",
        "side": "BUY",
        "quantity": 12,
        "price": 3080.0,
        "fees": 36.96,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-08-05",
        "symbol": "ITC",
        "isin": "INE154A01025",
        "exchange": "NSE",
        "side": "DIVIDEND",
        "quantity": 100,
        "price": 7.5,
        "amount": 750.0,
        "source": "demo_seed",
    },
    {
        "trade_date": "2024-12-10",
        "symbol": "SBIN",
        "isin": "INE062A01020",
        "exchange": "NSE",
        "side": "SELL",
        "quantity": 10,
        "price": 845.0,
        "fees": 12.0,
        "source": "demo_seed",
    },
]

_ABHISHEK_MF_HOLDINGS: list[dict[str, Any]] = [
    {
        "folio_number": "10293847",
        "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "amfi_code": "122639",
        "isin": "INF879O01027",
        "units": 1250.5,
        "avg_nav": 68.40,
        "invested_value": 85534.20,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {
            "category": "Flexi Cap",
            "constituents_source": "demo_seed",
            "constituents_confidence": "high",
            "constituents": {
                "HDFCBANK": 8.1,
                "ICICIBANK": 5.4,
                "ITC": 4.8,
                "TCS": 3.9,
                "INFY": 3.5,
                "POWERGRID": 6.2,
            },
        },
    },
    {
        "folio_number": "55443322",
        "scheme_name": "UTI Nifty 50 Index Fund - Direct Plan - Growth",
        "amfi_code": "120716",
        "isin": "INF789F01XA0",
        "units": 620.0,
        "avg_nav": 142.50,
        "invested_value": 88350.00,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {
            "category": "Index / ETF",
            "constituents_source": "demo_seed",
            "constituents_confidence": "high",
            "constituents": {
                "HDFCBANK": 11.5,
                "RELIANCE": 9.2,
                "ICICIBANK": 8.1,
                "INFY": 6.0,
                "TCS": 4.1,
                "ITC": 4.0,
                "SBIN": 3.1,
            },
        },
    },
    {
        "folio_number": "99887766",
        "scheme_name": "HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth",
        "amfi_code": "118989",
        "isin": "INF179K01XQ0",
        "units": 410.0,
        "avg_nav": 155.00,
        "invested_value": 63550.00,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {
            "category": "Mid Cap",
            "constituents_source": "demo_seed",
            "constituents_confidence": "high",
            "constituents": {
                "PERSISTENT": 3.8,
                "TATAMOTORS": 2.4,
                "COFORGE": 3.4,
                "MAXHEALTH": 3.6,
            },
        },
    },
]

_TANU_MF_HOLDINGS: list[dict[str, Any]] = [
    {
        "folio_number": "77665544",
        "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "amfi_code": "122639",
        "isin": "INF879O01027",
        "units": 850.0,
        "avg_nav": 71.20,
        "invested_value": 60520.00,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {
            "category": "Flexi Cap",
            "constituents_source": "demo_seed",
            "constituents_confidence": "high",
            "constituents": {
                "HDFCBANK": 8.1,
                "ICICIBANK": 5.4,
                "ITC": 4.8,
                "TCS": 3.9,
                "INFY": 3.5,
                "POWERGRID": 6.2,
            },
        },
    },
    {
        "folio_number": "33221100",
        "scheme_name": "Mirae Asset Large Cap Fund - Direct Plan - Growth",
        "amfi_code": "118825",
        "isin": "INF769K01AX2",
        "units": 700.0,
        "avg_nav": 96.50,
        "invested_value": 67550.00,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {
            "category": "Large Cap",
            "constituents_source": "demo_seed",
            "constituents_confidence": "high",
            "constituents": {
                "HDFCBANK": 9.8,
                "ICICIBANK": 8.5,
                "RELIANCE": 7.4,
                "INFY": 6.8,
                "SBIN": 3.6,
            },
        },
    },
    {
        "folio_number": "11223344",
        "scheme_name": "HDFC Liquid Fund - Direct Plan - Growth",
        "amfi_code": "119091",
        "isin": "INF179KB1HK0",
        "units": 12.5,
        "avg_nav": 4650.00,
        "invested_value": 58125.00,
        "as_of_date": "2026-11-20",
        "source": "demo_seed",
        "extra_data": {"category": "Debt / Non-Equity"},
    },
]


def _reset_database(db_path: Path) -> None:
    with get_connection(db_path) as conn:
        for table in (
            "import_logs",
            "transactions",
            "holdings",
            "mf_holdings",
            "symbol_aliases",
            "accounts",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _populate_company_names(db_path: Path) -> None:
    name_map = {item["raw_symbol"]: item["company_name"] for item in _DEMO_SYMBOLS}
    with get_connection(db_path) as conn:
        for sym, cname in name_map.items():
            conn.execute(
                "UPDATE holdings SET company_name = ? WHERE UPPER(symbol) = ? AND company_name = ''",
                (cname, sym),
            )
        conn.commit()


def seed_demo_data(db_path: Path | str | None = None, reset: bool = False) -> dict[str, Any]:
    """Populate realistic sample Indian equities, transactions, and mutual funds.

    Args:
        db_path: Target SQLite database path (defaults to `config.DB_PATH`).
        reset: When True, clears all existing portfolio tables before seeding.

    Returns:
        Summary dictionary with created accounts, transaction counts, and holdings counts.
    """
    target_path = Path(db_path) if db_path else Path(DB_PATH)
    init_db(db_path=target_path, seed_default_owners=True)
    if reset:
        _reset_database(target_path)
        init_db(db_path=target_path, seed_default_owners=True)

    for alias in _DEMO_SYMBOLS:
        save_symbol_alias(db_path=target_path, **alias)

    abhishek_acc = get_or_create_account("Abhishek", broker="5paisa", db_path=target_path)
    tanu_acc = get_or_create_account("Tanu", broker="5paisa", db_path=target_path)

    ab_tx = insert_transactions(
        account_id=abhishek_acc["id"],
        transactions=_ABHISHEK_TRANSACTIONS,
        source="demo_seed",
        filename="seed_demo_abhishek",
        db_path=target_path,
    )
    ta_tx = insert_transactions(
        account_id=tanu_acc["id"],
        transactions=_TANU_TRANSACTIONS,
        source="demo_seed",
        filename="seed_demo_tanu",
        db_path=target_path,
    )

    ab_hold = rebuild_holdings_from_transactions(account_id=abhishek_acc["id"], db_path=target_path)
    ta_hold = rebuild_holdings_from_transactions(account_id=tanu_acc["id"], db_path=target_path)
    _populate_company_names(target_path)

    ab_mf = upsert_mf_holdings(
        account_id=abhishek_acc["id"],
        mf_holdings=_ABHISHEK_MF_HOLDINGS,
        replace_account_mf=reset,
        source="demo_seed",
        filename="seed_demo_abhishek_mf",
        db_path=target_path,
    )
    ta_mf = upsert_mf_holdings(
        account_id=tanu_acc["id"],
        mf_holdings=_TANU_MF_HOLDINGS,
        replace_account_mf=reset,
        source="demo_seed",
        filename="seed_demo_tanu_mf",
        db_path=target_path,
    )

    return {
        "db_path": str(target_path),
        "reset": reset,
        "owners": ["Abhishek", "Tanu"],
        "accounts": {
            "Abhishek": abhishek_acc["id"],
            "Tanu": tanu_acc["id"],
        },
        "transactions": {
            "Abhishek": ab_tx,
            "Tanu": ta_tx,
        },
        "holdings": {
            "Abhishek": ab_hold,
            "Tanu": ta_hold,
        },
        "mf_holdings": {
            "Abhishek": ab_mf,
            "Tanu": ta_mf,
        },
        "aliases_seeded": len(_DEMO_SYMBOLS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed sample Indian stock & mutual fund data for Abhishek and Tanu."
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional path to SQLite database (defaults to config.DB_PATH).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear existing data in the target database before seeding.",
    )
    args = parser.parse_args()
    summary = seed_demo_data(db_path=args.db_path, reset=args.reset)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
