"""Unit and integration tests for the ingestion package."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from core.db import query_holdings, query_mf_holdings, query_transactions
from ingestion import (
    add_manual_trades,
    import_cams_cas,
    import_fivepaisa_file,
    parse_cams_cas,
    parse_fivepaisa_file,
    parse_manual_trades,
)


def _make_xlsx_bytes(rows: list[list[object]], sheet_name: str = "Sheet1") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_simple_pdf_bytes(lines: list[str]) -> bytes:
    """Create a minimal valid single-page PDF containing text lines for pdfplumber tests."""
    # Escape parentheses and backslashes in PDF literal strings
    escaped = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    content_ops = ["BT", "/F1 10 Tf", "40 760 Td", "14 TL"]
    for idx, ln in enumerate(escaped):
        if idx == 0:
            content_ops.append(f"({ln}) Tj")
        else:
            content_ops.append(f"T* ({ln}) Tj")
    content_ops.append("ET")
    stream_data = "\n".join(content_ops).encode("latin-1")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        (
            f"4 0 obj\n<< /Length {len(stream_data)} >>\nstream\n".encode("latin-1")
            + stream_data
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n".encode("latin-1"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return bytes(out)


def test_parse_fivepaisa_tradebook_csv_with_metadata_and_extra_cols():
    csv_text = (
        "5paisa Capital Ltd\n"
        "Client Code: 51234567\n"
        "Report: Equity Tradebook\n"
        "Period: 01-11-2026 to 25-11-2026\n"
        "\n"
        "Trade Date,Scrip Name,ISIN,Exchange,Buy/Sell,Qty,Avg Price,Brokerage,Order No,Segment Code\n"
        "20-11-2026,RELIANCE-EQ,INE002A01018,NSE_CASH,B,10,2950.50,15.25,ORD1001,EQ\n"
        "21/11/2026,TCS.NS,INE467B01029,BSE,SELL,5,4100.00,12.00,ORD1002,EQ\n"
        "22-Nov-2026,INFY - BE,,NSE,Dividend,100,18.50,0,ORD1003,EQ\n"
        "Total,,,,,,,27.25,,\n"
    )
    parsed = parse_fivepaisa_file(io.BytesIO(csv_text.encode("utf-8")), "tradebook.csv")
    assert parsed["kind"] == "transactions"
    assert parsed["extra_columns"] == ["Order No", "Segment Code"]
    assert len(parsed["records"]) == 3

    r0, r1, r2 = parsed["records"]
    assert r0["trade_date"] == "2026-11-20"
    assert r0["symbol"] == "RELIANCE"
    assert r0["exchange"] == "NSE"
    assert r0["side"] == "BUY"
    assert r0["quantity"] == 10.0
    assert r0["price"] == 2950.5
    assert r0["amount"] == 29505.0
    assert r0["fees"] == 15.25
    assert r0["extra_data"] == {"Order No": "ORD1001", "Segment Code": "EQ"}

    assert r1["trade_date"] == "2026-11-21"
    assert r1["symbol"] == "TCS"
    assert r1["exchange"] == "BSE"
    assert r1["side"] == "SELL"

    assert r2["trade_date"] == "2026-11-22"
    assert r2["symbol"] == "INFY"
    assert r2["side"] == "DIVIDEND"


def test_parse_fivepaisa_holdings_xlsx_and_import(tmp_path: Path):
    rows = [
        ["5paisa Holdings Statement"],
        ["Client: Abhishek"],
        ["As On: 24-11-2026"],
        [],
        ["Scrip", "Company Name", "ISIN Code", "Exchange", "Net Qty", "Avg. Price", "Invested Value", "DP ID"],
        ["HDFCBANK-EQ", "HDFC Bank Ltd", "INE040A01034", "NSE", 20, 1650.25, 33005.0, "12082500"],
        ["SBIN.NS", "State Bank of India", "INE062A01020", "NSE", 50, 780.0, None, "12082500"],
        ["Grand Total", "", "", "", 70, "", 72005.0, ""],
    ]
    xlsx_path = tmp_path / "holdings.xlsx"
    xlsx_path.write_bytes(_make_xlsx_bytes(rows))

    parsed = parse_fivepaisa_file(xlsx_path, "holdings.xlsx")
    assert parsed["kind"] == "holdings"
    assert parsed["extra_columns"] == ["DP ID"]
    assert len(parsed["records"]) == 2
    assert parsed["records"][0]["symbol"] == "HDFCBANK"
    assert parsed["records"][0]["company_name"] == "HDFC Bank Ltd"
    assert parsed["records"][0]["as_of_date"] == "2026-11-24"
    assert parsed["records"][1]["invested_value"] == 39000.0

    db_path = tmp_path / "test.db"
    res = import_fivepaisa_file(
        xlsx_path,
        filename="holdings.xlsx",
        owner="Abhishek",
        replace_holdings=True,
        db_path=db_path,
    )
    assert res["kind"] == "holdings"
    assert res["upserted"] == 2
    db_holdings = query_holdings(owner="Abhishek", db_path=db_path)
    assert len(db_holdings) == 2
    assert db_holdings[0]["extra_data"] == {"DP ID": "12082500"}


def test_parse_fivepaisa_html_xls_and_pdf(tmp_path: Path):
    html_xls = (
        "<html><body><table>"
        "<tr><th colspan='5'>5paisa Holdings</th></tr>"
        "<tr><th>Symbol</th><th>Qty</th><th>Avg Price</th><th>Sector</th></tr>"
        "<tr><td>ITC-EQ</td><td>100</td><td>420.5</td><td>FMCG</td></tr>"
        "</table></body></html>"
    ).encode("utf-8")
    parsed_xls = parse_fivepaisa_file(html_xls, "holdings.xls")
    assert parsed_xls["kind"] == "holdings"
    assert parsed_xls["records"][0]["symbol"] == "ITC"
    assert parsed_xls["records"][0]["extra_data"] == {"Sector": "FMCG"}

    pdf_bytes = _make_simple_pdf_bytes(
        [
            "5paisa Equity Tradebook",
            "Client: Tanu",
            "Trade Date   Symbol       Side   Qty   Price     Brokerage",
            "20-11-2026   RELIANCE-EQ  BUY    10    2950.00   12.50",
            "21-11-2026   TCS-EQ       SELL   5     4100.00   10.00",
        ]
    )
    parsed_pdf = parse_fivepaisa_file(pdf_bytes, "tradebook.pdf")
    assert parsed_pdf["kind"] == "transactions"
    assert len(parsed_pdf["records"]) == 2
    assert parsed_pdf["records"][0]["symbol"] == "RELIANCE"
    assert parsed_pdf["records"][1]["side"] == "SELL"


def test_import_fivepaisa_transactions_dedup_and_rebuild(tmp_path: Path):
    csv_bytes = (
        "Trade Date,Symbol,Side,Qty,Price\n"
        "2026-11-10,RELIANCE-EQ,BUY,10,2900\n"
        "2026-11-15,RELIANCE-EQ,BUY,10,3100\n"
        "2026-11-20,RELIANCE-EQ,SELL,5,3200\n"
    ).encode("utf-8")
    db_path = tmp_path / "test.db"

    res1 = import_fivepaisa_file(
        csv_bytes,
        filename="trades.csv",
        owner="Tanu",
        rebuild_holdings=True,
        db_path=db_path,
    )
    assert res1["inserted"] == 3
    assert res1["skipped"] == 0
    assert res1["rebuilt"]["upserted"] == 1

    # Re-importing overlapping CSV skips duplicates
    res2 = import_fivepaisa_file(
        csv_bytes,
        filename="trades.csv",
        owner="Tanu",
        db_path=db_path,
    )
    assert res2["inserted"] == 0
    assert res2["skipped"] == 3

    holdings = query_holdings(owner="Tanu", db_path=db_path)
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 15.0
    assert holdings[0]["avg_price"] == 3000.0


def test_parse_and_import_cams_cas_csv_and_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cams_csv = (
        "CAMS Mutual Fund Valuation Report\n"
        "Investor Name: Abhishek\n"
        "As on Date: 24-11-2026\n"
        "\n"
        "Folio No,Scheme Name,AMFI Code,ISIN,Units,Avg NAV,Invested Value,Current Value,Advisor,Registrar\n"
        "1234567/89,Parag Parikh Flexi Cap Fund - Direct Growth,122639,INF879O01027,150.5,65.00,9782.50,12491.50,DIRECT,CAMS\n"
        "9876543/21,HDFC Mid-Cap Opportunities Fund - Direct Growth,118989,INF179K01XQ0,200.0,140.00,28000.00,36000.00,ARN-123,CAMS\n"
    ).encode("utf-8")

    parsed_csv = parse_cams_cas(cams_csv, "cams_export.csv")
    assert len(parsed_csv["mf_holdings"]) == 2
    assert parsed_csv["mf_holdings"][0]["folio_number"] == "1234567/89"
    assert parsed_csv["mf_holdings"][0]["amfi_code"] == "122639"
    assert parsed_csv["mf_holdings"][0]["as_of_date"] == "2026-11-24"
    assert parsed_csv["mf_holdings"][0]["extra_data"]["Advisor"] == "DIRECT"

    # Mock casparser.read_cas_pdf for PDF test
    mock_cas_dict = {
        "statement_period": {"from": "01-Nov-2026", "to": "24-Nov-2026"},
        "investor_info": {"name": "Abhishek", "email": "a@example.com", "address": "", "mobile": ""},
        "folios": [
            {
                "folio": "44556677/01",
                "amc": "PPFAS Mutual Fund",
                "name": "Abhishek",
                "PAN": "ABCDE1234F",
                "KYC": "OK",
                "schemes": [
                    {
                        "scheme": "Parag Parikh Flexi Cap Fund - Direct Plan Growth",
                        "advisor": "DIRECT",
                        "rta_code": "PPFCGD",
                        "rta": "CAMS",
                        "type": "EQUITY",
                        "isin": "INF879O01027",
                        "amfi": "122639",
                        "nominees": ["Tanu"],
                        "open": Decimal("100.0"),
                        "close": Decimal("150.0"),
                        "close_calculated": Decimal("150.0"),
                        "valuation": {
                            "date": date(2026, 11, 24),
                            "nav": Decimal("85.00"),
                            "cost": Decimal("10500.00"),
                            "value": Decimal("12750.00"),
                        },
                        "transactions": [
                            {
                                "date": date(2026, 11, 5),
                                "description": "Systematic Investment",
                                "amount": Decimal("5000.00"),
                                "units": Decimal("50.0"),
                                "nav": Decimal("100.0"),
                                "balance": Decimal("150.0"),
                                "type": "PURCHASE_SIP",
                            }
                        ],
                    }
                ],
            }
        ],
        "parse_warnings": [],
    }
    monkeypatch.setattr("ingestion.cams_cas.casparser.read_cas_pdf", lambda *args, **kwargs: mock_cas_dict)

    db_path = tmp_path / "mf.db"
    res = import_cams_cas(b"%PDF-1.4 dummy", "cas.pdf", owner="Abhishek", password="secret", db_path=db_path)
    assert res["upserted"] == 1
    assert res["mf_transactions_count"] == 1

    mf_rows = query_mf_holdings(owner="Abhishek", db_path=db_path)
    assert len(mf_rows) == 1
    assert mf_rows[0]["folio_number"] == "44556677/01"
    assert mf_rows[0]["units"] == 150.0
    assert mf_rows[0]["avg_nav"] == 70.0
    assert mf_rows[0]["invested_value"] == 10500.0
    assert mf_rows[0]["extra_data"]["registrar"] == "CAMS"
    assert mf_rows[0]["extra_data"]["valuation"] == 12750.0


def test_parse_cams_cas_pdfplumber_fallback(monkeypatch: pytest.MonkeyPatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated casparser error")

    monkeypatch.setattr("ingestion.cams_cas.casparser.read_cas_pdf", _boom)

    pdf_bytes = _make_simple_pdf_bytes(
        [
            "Consolidated Account Statement",
            "01-Nov-2026 To 24-Nov-2026",
            "Abhishek Kumar",
            "Folio No: 778899/12  PAN: ABCDE1234F",
            "Quant Small Cap Fund - Direct Growth - ISIN: INF966L01689 (Advisor: DIRECT)",
            "Registrar : KFINTECH",
            "Closing Unit Balance: 250.000",
            "Total Cost Value: 50,000.00",
            "NAV on 24-Nov-2026: INR 260.00",
            "Market Value on 24-Nov-2026: INR 65,000.00",
        ]
    )
    parsed = parse_cams_cas(pdf_bytes, "fallback_cas.pdf")
    assert len(parsed["mf_holdings"]) == 1
    h = parsed["mf_holdings"][0]
    assert h["folio_number"] == "778899/12"
    assert h["isin"] == "INF966L01689"
    assert h["units"] == 250.0
    assert h["invested_value"] == 50000.0
    assert h["avg_nav"] == 200.0
    assert h["extra_data"]["registrar"] == "KFINTECH"
    assert h["extra_data"]["valuation"] == 65000.0


def test_manual_delta_shorthand_and_csv(tmp_path: Path):
    text = """
    # Quick trades
    2026-11-20 BUY RELIANCE-EQ 10 @ 2950
    2026-11-21 BONUS RELIANCE 10
    SELL TCS.NS 5 4100 BSE
    2026-11-22 SPLIT TATAMOTORS 25
    2026-11-23 DIVIDEND ITC 100 @ 6.25
    """
    trades = parse_manual_trades(text)
    assert len(trades) == 5

    assert trades[0]["trade_date"] == "2026-11-20"
    assert trades[0]["side"] == "BUY"
    assert trades[0]["symbol"] == "RELIANCE"
    assert trades[0]["quantity"] == 10.0
    assert trades[0]["price"] == 2950.0
    assert trades[0]["amount"] == 29500.0

    assert trades[1]["side"] == "BONUS"
    assert trades[1]["quantity"] == 10.0
    assert trades[1]["price"] == 0.0

    assert trades[2]["side"] == "SELL"
    assert trades[2]["symbol"] == "TCS"
    assert trades[2]["exchange"] == "BSE"
    assert trades[2]["quantity"] == 5.0
    assert trades[2]["price"] == 4100.0

    assert trades[3]["side"] == "SPLIT"
    assert trades[3]["symbol"] == "TATAMOTORS"
    assert trades[3]["quantity"] == 25.0

    assert trades[4]["side"] == "DIVIDEND"
    assert trades[4]["amount"] == 625.0

    # Also verify headered CSV paste
    pasted_csv = "Trade Date,Scrip,Side,Qty,Price,Notes\n2026-11-24,INFY-EQ,BUY,8,1850,SIP\n"
    csv_trades = parse_manual_trades(pasted_csv)
    assert len(csv_trades) == 1
    assert csv_trades[0]["symbol"] == "INFY"
    assert csv_trades[0]["extra_data"] == {"Notes": "SIP"}

    db_path = tmp_path / "manual.db"
    res = add_manual_trades(
        "2026-11-20 BUY RELIANCE 10 @ 3000\n2026-11-21 BONUS RELIANCE 10",
        owner="Abhishek",
        rebuild_holdings=True,
        db_path=db_path,
    )
    assert res["inserted"] == 2
    assert res["rebuilt"]["upserted"] == 1

    txs = query_transactions(owner="Abhishek", db_path=db_path)
    assert len(txs) == 2
    holds = query_holdings(owner="Abhishek", db_path=db_path)
    assert holds[0]["quantity"] == 20.0
    assert holds[0]["avg_price"] == 1500.0


def test_holding_baseline_preserved_across_rebuild(tmp_path: Path):
    from core.db import get_or_create_account, init_db, rebuild_holdings_from_transactions, upsert_holding_baselines

    db_path = tmp_path / "baseline.db"
    init_db(db_path=db_path, seed_default_owners=False)
    acc = get_or_create_account("Abhishek", broker="5paisa", db_path=db_path)

    # Pre-baseline tradebook entry (e.g. partial history) and depository baseline as of 2026-11-25
    add_manual_trades(
        "2026-11-10 BUY MON100 100 @ 120.0",
        owner="Abhishek",
        rebuild_holdings=False,
        db_path=db_path,
    )
    upsert_holding_baselines(
        account_id=acc["id"],
        holdings=[
            {
                "symbol": "MON100",
                "isin": "INF247L01AP3",
                "company_name": "Motilal Oswal NASDAQ 100 ETF",
                "quantity": 163600.0,
                "avg_price": 109.94,
                "invested_value": 17986184.0,
                "as_of_date": "2026-11-25",
            }
        ],
        db_path=db_path,
    )
    rebuild_holdings_from_transactions(account_id=acc["id"], db_path=db_path)
    holds = query_holdings(owner="Abhishek", db_path=db_path)
    assert len(holds) == 1
    assert holds[0]["quantity"] == 163600.0
    assert holds[0]["avg_price"] == 109.94

    # Post-baseline trade applies cleanly on top of the baseline
    add_manual_trades(
        "2026-11-26 SELL MON100 1600 @ 295.0",
        owner="Abhishek",
        rebuild_holdings=True,
        db_path=db_path,
    )
    holds_after = query_holdings(owner="Abhishek", db_path=db_path)
    assert len(holds_after) == 1
    assert holds_after[0]["quantity"] == 162000.0
    assert holds_after[0]["as_of_date"] == "2026-11-26"


def test_advisory_source_columns_and_update(tmp_path: Path):
    from core.db import (
        get_or_create_account,
        init_db,
        query_mf_holdings,
        rebuild_holdings_from_transactions,
        update_advisory_source,
        upsert_holding_baselines,
        upsert_mf_holdings,
    )

    db_path = tmp_path / "advisory.db"
    init_db(db_path=db_path, seed_default_owners=False)
    acc = get_or_create_account("Abhishek", broker="5paisa", db_path=db_path)

    upsert_holding_baselines(
        account_id=acc["id"],
        holdings=[
            {"symbol": "MON100", "quantity": 100, "avg_price": 109.94, "as_of_date": "2026-11-25"},
            {"symbol": "ICICI BANK", "quantity": 50, "avg_price": 1384.10, "as_of_date": "2026-11-25"},
        ],
        db_path=db_path,
    )
    rebuild_holdings_from_transactions(account_id=acc["id"], db_path=db_path)
    upsert_mf_holdings(
        account_id=acc["id"],
        mf_holdings=[{"scheme_name": "Capitalmind Flexi Cap Fund", "units": 1000.0, "as_of_date": "2026-11-25"}],
        db_path=db_path,
    )

    # Default is 'Self'
    eqs = {h["symbol"]: h for h in query_holdings(owner="Abhishek", db_path=db_path)}
    assert eqs["MON100"]["advisory_source"] == "Self"

    # Update advisory source and verify rebuild preserves it
    update_advisory_source(
        account_id=acc["id"],
        symbol_or_scheme="ICICI BANK",
        advisory_source="ValueResearch",
        advisory_notes="Core banking pick",
        asset_class="EQUITY",
        db_path=db_path,
    )
    update_advisory_source(
        account_id=acc["id"],
        symbol_or_scheme="Capitalmind Flexi Cap Fund",
        advisory_source="Capitalmind",
        advisory_notes="PMS / Flexi",
        asset_class="MF",
        db_path=db_path,
    )

    rebuild_holdings_from_transactions(account_id=acc["id"], db_path=db_path)
    eqs_after = {h["symbol"]: h for h in query_holdings(owner="Abhishek", db_path=db_path)}
    assert eqs_after["ICICI BANK"]["advisory_source"] == "ValueResearch"
    assert eqs_after["ICICI BANK"]["advisory_notes"] == "Core banking pick"
    assert eqs_after["MON100"]["advisory_source"] == "Self"

    mfs_after = query_mf_holdings(owner="Abhishek", db_path=db_path)
    assert mfs_after[0]["advisory_source"] == "Capitalmind"
    assert mfs_after[0]["advisory_notes"] == "PMS / Flexi"


def test_server_control_detect_port_and_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys
    from unittest.mock import MagicMock
    from core import server_control

    monkeypatch.setattr(sys, "argv", ["streamlit", "run", "app.py", "--server.port", "8512"])
    assert server_control._detect_port() == 8512
    monkeypatch.setattr(sys, "argv", ["streamlit", "run", "app.py", "--server.port=8519"])
    assert server_control._detect_port() == 8519

    mock_popen = MagicMock()
    monkeypatch.setattr(server_control.subprocess, "Popen", mock_popen)
    log_path = server_control.schedule_server_restart(
        port=8512,
        delay_seconds=0.1,
        log_path=tmp_path / "streamlit.log",
    )
    assert log_path == tmp_path / "streamlit.log"
    assert mock_popen.called


def test_region_marking_and_segment_benchmark_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import pandas as pd
    from core import analytics
    from core.db import (
        get_or_create_account,
        init_db,
        rebuild_holdings_from_transactions,
        update_advisory_source,
        update_asset_region,
        upsert_holding_baselines,
        upsert_mf_holdings,
    )

    db_path = tmp_path / "region.db"
    init_db(db_path=db_path, seed_default_owners=False)
    acc = get_or_create_account("Abhishek", broker="5paisa", db_path=db_path)

    upsert_holding_baselines(
        account_id=acc["id"],
        holdings=[
            {"symbol": "RELIANCE", "quantity": 100.0, "avg_price": 1200.0, "as_of_date": "2026-11-25"},
            {"symbol": "MON100", "quantity": 500.0, "avg_price": 109.94, "as_of_date": "2026-11-25"},
        ],
        db_path=db_path,
    )
    rebuild_holdings_from_transactions(account_id=acc["id"], db_path=db_path)
    upsert_mf_holdings(
        account_id=acc["id"],
        mf_holdings=[{"scheme_name": "ICICI Prudential NASDAQ 100 Index Fund", "units": 100.0, "as_of_date": "2026-11-25"}],
        db_path=db_path,
    )

    update_asset_region(acc["id"], "MON100", region="Foreign", asset_class="EQUITY", db_path=db_path)
    update_asset_region(acc["id"], "ICICI Prudential NASDAQ 100 Index Fund", region="Foreign", asset_class="MF", db_path=db_path)
    update_advisory_source(acc["id"], "RELIANCE", advisory_source="Advisor One", asset_class="EQUITY", db_path=db_path)

    holds = {h["symbol"]: h for h in query_holdings(owner="Abhishek", db_path=db_path)}
    assert holds["MON100"]["region"] == "Foreign"
    assert holds["RELIANCE"]["region"] == "India"
    assert holds["RELIANCE"]["advisory_source"] == "Advisor One"
    mfs = query_mf_holdings(owner="Abhishek", db_path=db_path)
    assert mfs[0]["region"] == "Foreign"

    dates = pd.to_datetime(["2026-11-20", "2026-11-24", "2026-11-25"])

    def _fake_hist(symbols, start_date=None, end_date=None, db_path=None):
        data = {s: [100.0, 110.0, 120.0] if s == "RELIANCE" else [200.0, 220.0, 240.0] for s in symbols}
        return pd.DataFrame(data, index=dates)

    monkeypatch.setattr(analytics, "fetch_historical_prices", _fake_hist)
    monkeypatch.setattr(
        analytics,
        "fetch_benchmark_series",
        lambda benchmark_key="NIFTY_50", start_date=None, end_date=None: pd.Series([1000.0, 1020.0, 1050.0], index=dates),
    )

    overall = analytics.compare_portfolio_vs_benchmark(owner="Abhishek", start_date="2026-11-20", end_date="2026-11-25", adjust_for_splits=False, db_path=db_path)
    india = analytics.compare_portfolio_vs_benchmark(owner="Abhishek", start_date="2026-11-20", end_date="2026-11-25", adjust_for_splits=False, region="India", db_path=db_path)
    foreign = analytics.compare_portfolio_vs_benchmark(owner="Abhishek", start_date="2026-11-20", end_date="2026-11-25", adjust_for_splits=False, region="Foreign", db_path=db_path)
    adv1 = analytics.compare_portfolio_vs_benchmark(owner="Abhishek", start_date="2026-11-20", end_date="2026-11-25", adjust_for_splits=False, advisory_source="Advisor One", db_path=db_path)

    assert overall["included_symbols"] == ["MON100", "RELIANCE"]
    assert india["included_symbols"] == ["RELIANCE"]
    assert foreign["included_symbols"] == ["MON100"]
    assert adv1["included_symbols"] == ["RELIANCE"]
    assert overall["summary"]["portfolio_end_value"] == pytest.approx(100 * 120 + 500 * 240)
    assert india["summary"]["portfolio_end_value"] == pytest.approx(100 * 120)
    assert foreign["summary"]["portfolio_end_value"] == pytest.approx(500 * 240)


def test_other_assets_and_sif_usd_inr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify static other_assets CRUD, SIF equity categorization, and USD->INR FX conversion."""
    import core.analytics as analytics
    from core.db import get_or_create_account, init_db, query_other_assets, save_other_assets, upsert_holdings

    db_path = tmp_path / "alt_sif.db"
    init_db(db_path)

    # 1. Alternative & Fixed Assets CRUD & scoped replacement
    res = save_other_assets(
        [
            {"owner": "Abhishek", "name": "Protean NPS (Tier I)", "category": "Retirement & PPF", "invested_value": 400000, "current_value": 550000},
            {"owner": "Abhishek", "name": "Lucknow Land", "category": "Real Estate & Land", "invested_value": 1000000, "current_value": 1500000},
            {"owner": "Tanu", "name": "Tanu PPF", "category": "Retirement & PPF", "invested_value": 200000, "current_value": 240000},
        ],
        owner=None,
        replace_scope=True,
        db_path=db_path,
    )
    assert res["saved"] == 3
    all_alt = query_other_assets(db_path=db_path)
    assert len(all_alt) == 3
    abh_alt = query_other_assets(owner="Abhishek", db_path=db_path)
    assert len(abh_alt) == 2

    # Updating only Abhishek's slice leaves Tanu's row intact
    save_other_assets(
        [{"owner": "Abhishek", "name": "Protean NPS (Tier I)", "category": "Retirement & PPF", "invested_value": 450000, "current_value": 600000}],
        owner="Abhishek",
        replace_scope=True,
        db_path=db_path,
    )
    assert len(query_other_assets(owner="Abhishek", db_path=db_path)) == 1
    assert len(query_other_assets(owner="Tanu", db_path=db_path)) == 1

    # 2. SIF equity category + USD->INR foreign fund conversion
    acc = get_or_create_account("Abhishek", broker="Manual", account_name="Abhishek - Special Funds", db_path=db_path)
    upsert_holdings(
        account_id=acc["id"],
        holdings=[
            {
                "symbol": "ALTIVA_SIF",
                "company_name": "Altiva Hybrid Long-Short SIF",
                "asset_type": "SIF",
                "quantity": 1000.0,
                "avg_price": 100.0,
                "as_of_date": "2026-11-25",
                "region": "India",
                "advisory_source": "Self",
                "extra_data": {"manual_ltp": 112.5},
            },
            {
                "symbol": "PPFAS_GIFT",
                "company_name": "PPFAS GIFT City Overseas Fund",
                "asset_type": "EQUITY",
                "quantity": 100.0,
                "avg_price": 10.0,
                "as_of_date": "2026-11-25",
                "region": "Foreign",
                "advisory_source": "Self",
                "extra_data": {"cost_currency": "USD", "live_currency": "USD", "manual_ltp": 12.0},
            },
        ],
        db_path=db_path,
    )

    monkeypatch.setattr(analytics, "fetch_latest_prices", lambda syms, db_path=None: {})
    monkeypatch.setattr(analytics, "fetch_usdinr_rate", lambda: 85.0)

    snap = analytics.get_portfolio_snapshot(owner="Abhishek", db_path=db_path)
    by_sym = {h["symbol"]: h for h in snap["equity_holdings"]}
    assert by_sym["ALTIVA_SIF"]["asset_type"] == "SIF"
    assert by_sym["ALTIVA_SIF"]["invested_value"] == pytest.approx(100000.0)
    assert by_sym["ALTIVA_SIF"]["current_value"] == pytest.approx(112500.0)
    assert by_sym["PPFAS_GIFT"]["currency"] == "INR"
    assert by_sym["PPFAS_GIFT"]["invested_value"] == pytest.approx(85000.0)
    assert by_sym["PPFAS_GIFT"]["current_value"] == pytest.approx(102000.0)
    assert snap["totals"]["equity_current_value"] == pytest.approx(214500.0)
    assert snap["totals"]["holdings_count"] == 2
