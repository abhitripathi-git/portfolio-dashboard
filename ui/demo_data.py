"""Safe sample data seeder for exploring the dashboard without touching real data."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import DATA_DIR
from core.db import (
    get_or_create_account,
    init_db,
    insert_transactions,
    rebuild_holdings_from_transactions,
    save_symbol_alias,
    upsert_mf_holdings,
)

DEMO_DB_PATH = DATA_DIR / "demo_portfolio.db"


def seed_demo_database(db_path: Path | str = DEMO_DB_PATH) -> dict[str, Any]:
    """Populate `db_path` with realistic sample family data for Abhishek and Tanu."""
    init_db(db_path=db_path, seed_default_owners=True)

    abhi_eq = get_or_create_account("Abhishek", broker="5paisa", account_name="Abhishek - 5paisa", db_path=db_path)
    tanu_eq = get_or_create_account("Tanu", broker="5paisa", account_name="Tanu - 5paisa", db_path=db_path)
    abhi_mf = get_or_create_account("Abhishek", broker="cams_cas", account_name="Abhishek - cams_cas", db_path=db_path)
    tanu_mf = get_or_create_account("Tanu", broker="cams_cas", account_name="Tanu - cams_cas", db_path=db_path)

    today = date.today()
    d = lambda days_ago: (today - timedelta(days=days_ago)).isoformat()

    abhi_txs = [
        {
            "trade_date": d(330), "symbol": "RELIANCE", "isin": "INE002A01018",
            "side": "BUY", "quantity": 20, "price": 1280.0, "fees": 35.0,
            "extra_data": {"Sector": "Energy", "Conviction": "Core"},
        },
        {
            "trade_date": d(270), "symbol": "HDFCBANK", "isin": "INE040A01034",
            "side": "BUY", "quantity": 30, "price": 1540.0, "fees": 45.0,
            "extra_data": {"Sector": "Banking", "Conviction": "Core"},
        },
        {
            "trade_date": d(210), "symbol": "TCS", "isin": "INE467B01029",
            "side": "BUY", "quantity": 12, "price": 3820.0, "fees": 40.0,
            "extra_data": {"Sector": "IT", "Conviction": "Core"},
        },
        {
            "trade_date": d(150), "symbol": "INFY", "isin": "INE009A01021",
            "side": "BUY", "quantity": 25, "price": 1620.0, "fees": 32.0,
            "extra_data": {"Sector": "IT", "Conviction": "Growth"},
        },
        {
            "trade_date": d(95), "symbol": "ICICIBANK", "isin": "INE090A01021",
            "side": "BUY", "quantity": 35, "price": 1180.0, "fees": 38.0,
            "extra_data": {"Sector": "Banking", "Conviction": "Core"},
        },
        {
            "trade_date": d(45), "symbol": "RELIANCE", "isin": "INE002A01018",
            "side": "BUY", "quantity": 10, "price": 1310.0, "fees": 22.0,
            "extra_data": {"Sector": "Energy", "Conviction": "Core"},
        },
        {
            "trade_date": d(30), "symbol": "TCS", "isin": "INE467B01029",
            "side": "DIVIDEND", "quantity": 12, "price": 28.0, "amount": 336.0, "fees": 0.0,
            "extra_data": {"Sector": "IT"},
        },
    ]

    tanu_txs = [
        {
            "trade_date": d(310), "symbol": "HDFCBANK", "isin": "INE040A01034",
            "side": "BUY", "quantity": 22, "price": 1510.0, "fees": 30.0,
            "extra_data": {"Sector": "Banking", "Goal": "Long Term"},
        },
        {
            "trade_date": d(220), "symbol": "SBIN", "isin": "INE062A01020",
            "side": "BUY", "quantity": 45, "price": 760.0, "fees": 28.0,
            "extra_data": {"Sector": "Banking", "Goal": "Value"},
        },
        {
            "trade_date": d(140), "symbol": "INFY", "isin": "INE009A01021",
            "side": "BUY", "quantity": 18, "price": 1590.0, "fees": 25.0,
            "extra_data": {"Sector": "IT", "Goal": "Long Term"},
        },
        {
            "trade_date": d(60), "symbol": "BHARTIARTL", "isin": "INE397D01024",
            "side": "BUY", "quantity": 25, "price": 1550.0, "fees": 30.0,
            "extra_data": {"Sector": "Telecom", "Goal": "Growth"},
        },
    ]

    r1 = insert_transactions(abhi_eq["id"], abhi_txs, source="demo", filename="demo_abhishek.csv", db_path=db_path)
    r2 = insert_transactions(tanu_eq["id"], tanu_txs, source="demo", filename="demo_tanu.csv", db_path=db_path)
    rebuild_holdings_from_transactions(abhi_eq["id"], db_path=db_path)
    rebuild_holdings_from_transactions(tanu_eq["id"], db_path=db_path)

    # Seed symbol aliases for instant resolution
    aliases = [
        ("RELIANCE", "RELIANCE.NS", "INE002A01018", "Reliance Industries Ltd", "Energy"),
        ("HDFCBANK", "HDFCBANK.NS", "INE040A01034", "HDFC Bank Ltd", "Banking"),
        ("TCS", "TCS.NS", "INE467B01029", "Tata Consultancy Services Ltd", "IT"),
        ("INFY", "INFY.NS", "INE009A01021", "Infosys Ltd", "IT"),
        ("ICICIBANK", "ICICIBANK.NS", "INE090A01021", "ICICI Bank Ltd", "Banking"),
        ("SBIN", "SBIN.NS", "INE062A01020", "State Bank of India", "Banking"),
        ("BHARTIARTL", "BHARTIARTL.NS", "INE397D01024", "Bharti Airtel Ltd", "Telecom"),
    ]
    for sym, ysym, isin, cname, sec in aliases:
        save_symbol_alias(sym, ysym, isin=isin, company_name=cname, sector=sec, db_path=db_path)

    abhi_mfs = [
        {
            "folio_number": "10293847/11",
            "scheme_name": "UTI Nifty 50 Index Fund - Direct Plan - Growth",
            "amfi_code": "120716",
            "isin": "INF789F01XA0",
            "units": 1450.5,
            "avg_nav": 132.4,
            "invested_value": 192046.20,
            "as_of_date": today.isoformat(),
            "extra_data": {
                "registrar": "KFINTECH",
                "plan": "Direct",
                "constituents_source": "demo_index_snapshot",
                "constituents_confidence": "high",
                "constituents": {
                    "HDFCBANK": 11.4,
                    "ICICIBANK": 8.3,
                    "RELIANCE": 8.9,
                    "INFY": 5.8,
                    "TCS": 3.9,
                    "BHARTIARTL": 3.8,
                    "LT": 4.1,
                    "ITC": 4.0,
                    "SBIN": 3.1,
                    "AXISBANK": 3.0,
                },
            },
        },
        {
            "folio_number": "44556677/02",
            "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
            "amfi_code": "122639",
            "isin": "INF879O01027",
            "units": 2100.0,
            "avg_nav": 64.5,
            "invested_value": 135450.00,
            "as_of_date": today.isoformat(),
            "extra_data": {
                "registrar": "CAMS",
                "plan": "Direct",
                "constituents_source": "demo_factsheet",
                "constituents_confidence": "high",
                "constituents": {
                    "HDFCBANK": 7.9,
                    "BAJFINANCE": 6.4,
                    "COALINDIA": 5.9,
                    "POWERGRID": 5.6,
                    "ITC": 5.1,
                    "ICICIBANK": 4.8,
                    "MARUTI": 4.2,
                },
            },
        },
    ]

    tanu_mfs = [
        {
            "folio_number": "88776655/09",
            "scheme_name": "HDFC Mid-Cap Opportunities Fund - Direct Plan - Growth",
            "amfi_code": "118989",
            "isin": "INF179K01XQ0",
            "units": 980.0,
            "avg_nav": 148.0,
            "invested_value": 145040.00,
            "as_of_date": today.isoformat(),
            "extra_data": {
                "registrar": "CAMS",
                "plan": "Direct",
            },
        },
        {
            "folio_number": "99881122/04",
            "scheme_name": "Nippon India Nifty Next 50 Junior BeES FoF - Direct - Growth",
            "amfi_code": "148980",
            "isin": "INF204K01HY8",
            "units": 4200.0,
            "avg_nav": 19.8,
            "invested_value": 83160.00,
            "as_of_date": today.isoformat(),
            "extra_data": {
                "registrar": "KFINTECH",
                "plan": "Direct",
            },
        },
    ]

    m1 = upsert_mf_holdings(abhi_mf["id"], abhi_mfs, replace_account_mf=True, source="demo", filename="demo_abhi_cas.pdf", db_path=db_path)
    m2 = upsert_mf_holdings(tanu_mf["id"], tanu_mfs, replace_account_mf=True, source="demo", filename="demo_tanu_cas.pdf", db_path=db_path)

    return {
        "abhishek_txs": r1,
        "tanu_txs": r2,
        "abhishek_mfs": m1,
        "tanu_mfs": m2,
    }
