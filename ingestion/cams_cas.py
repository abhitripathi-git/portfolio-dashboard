"""CAMS / KFintech CAS PDF and Excel/CSV mutual fund parser and SQLite importer."""

from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Any

import casparser
import pdfplumber

from core.db import get_or_create_account, init_db, insert_transactions, upsert_mf_holdings
from ingestion._utils import (
    FileInput,
    build_extra_data,
    detect_header_and_kind,
    extract_metadata_date,
    is_cell_empty,
    is_summary_or_total_row,
    load_tabular_sheets,
    normalize_date,
    normalize_header_text,
    normalize_side,
    parse_number,
    read_input_bytes,
)


MF_HOLDINGS_COLUMN_ALIASES: dict[str, list[str]] = {
    "folio_number": ["folio", "folio no", "folio number", "foliono", "folio id"],
    "scheme_name": ["scheme name", "scheme", "fund name", "fund", "scheme description"],
    "amfi_code": ["amfi", "amfi code", "scheme code", "amfi_code"],
    "isin": ["isin", "isin code", "isin no"],
    "units": [
        "units", "balance units", "closing units", "unit balance", "net units",
        "qty", "quantity", "closing balance",
    ],
    "avg_nav": [
        "avg nav", "average nav", "avg cost", "cost nav", "purchase nav",
        "avg purchase nav", "avg price",
    ],
    "invested_value": [
        "invested value", "invested", "cost value", "total cost", "purchase value",
        "invested amount", "cost", "amount invested",
    ],
    "as_of_date": ["as of date", "as on date", "valuation date", "nav date", "date"],
}

MF_TX_COLUMN_ALIASES: dict[str, list[str]] = {
    "trade_date": ["trade date", "date", "txn date", "transaction date", "nav date"],
    "folio_number": ["folio", "folio no", "folio number", "foliono"],
    "scheme_name": ["scheme name", "scheme", "fund name", "fund"],
    "isin": ["isin", "isin code"],
    "amfi_code": ["amfi", "amfi code", "scheme code"],
    "side": ["transaction type", "txn type", "type", "description", "particulars", "side"],
    "units": ["units", "unit", "qty", "quantity"],
    "nav": ["nav", "price", "rate", "purchase price"],
    "amount": ["amount", "value", "net amount", "gross amount"],
}

_CAS_TX_SIDE_MAP: dict[str, str] = {
    "PURCHASE": "BUY",
    "PURCHASE_SIP": "BUY",
    "SWITCH_IN": "BUY",
    "SWITCH_IN_MERGER": "BUY",
    "STP_IN": "BUY",
    "GIFT_IN": "BUY",
    "SEGREGATION": "BUY",
    "REDEMPTION": "SELL",
    "SWITCH_OUT": "SELL",
    "SWITCH_OUT_MERGER": "SELL",
    "STP_OUT": "SELL",
    "GIFT_OUT": "SELL",
    "DIVIDEND_PAYOUT": "DIVIDEND",
    "DIVIDEND_REINVEST": "DIVIDEND",
    "REVERSAL": "SELL",
}


def _map_cas_tx_side(raw_type: Any, description: str = "") -> str:
    t = str(raw_type or "").strip().upper()
    if t in _CAS_TX_SIDE_MAP:
        return _CAS_TX_SIDE_MAP[t]
    side = normalize_side(t) or normalize_side(description)
    return side or "BUY"


def _estimate_cost_from_transactions(transactions: list[dict[str, Any]]) -> float:
    """Compute remaining cost basis via weighted average when CAS omits cost."""
    units_bal = 0.0
    cost_bal = 0.0
    for tx in transactions:
        tx_type = str(tx.get("type") or "").upper()
        u = parse_number(tx.get("units"), default=0.0) or 0.0
        amt = parse_number(tx.get("amount"), default=0.0) or 0.0
        side = _map_cas_tx_side(tx_type, str(tx.get("description") or ""))
        if side == "BUY" and u > 0:
            units_bal += u
            cost_bal += abs(amt)
        elif side == "SELL" and abs(u) > 0 and units_bal > 0:
            sell_u = min(abs(u), units_bal)
            avg = cost_bal / units_bal
            units_bal -= sell_u
            cost_bal -= sell_u * avg
        elif tx_type == "DIVIDEND_REINVEST" and u > 0:
            units_bal += u
            cost_bal += abs(amt)
    return max(0.0, round(cost_bal, 2))


def _parse_casparser_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Convert casparser output dict (RTA or NSDL CAS) into normalized mf_holdings."""
    mf_holdings: list[dict[str, Any]] = []
    mf_transactions: list[dict[str, Any]] = []
    warnings = [str(w) for w in (data.get("parse_warnings") or []) if w]

    investor_info = dict(data.get("investor_info") or {})
    statement_period = dict(data.get("statement_period") or {})
    period_to = normalize_date(statement_period.get("to")) or date.today().isoformat()
    investor_name = str(investor_info.get("name") or "").strip()

    # Standard CAMS/KFintech RTA CAS structure
    for folio in data.get("folios") or []:
        folio_no = str(folio.get("folio") or "").strip()
        amc = str(folio.get("amc") or "").strip()
        holder_name = str(folio.get("name") or investor_name).strip()
        pan = str(folio.get("PAN") or "").strip()

        for scheme in folio.get("schemes") or []:
            scheme_name = re.sub(r"\s+", " ", str(scheme.get("scheme") or "")).strip()
            if not scheme_name:
                continue
            isin = str(scheme.get("isin") or "").strip().upper()
            amfi_code = str(scheme.get("amfi") or "").strip()
            units = parse_number(scheme.get("close"), default=None)
            if units is None:
                units = parse_number(scheme.get("close_calculated"), default=0.0) or 0.0

            valuation = scheme.get("valuation") or {}
            as_of_date = normalize_date(valuation.get("date")) or period_to
            val_nav = parse_number(valuation.get("nav"), default=0.0) or 0.0
            val_value = parse_number(valuation.get("value"), default=0.0) or 0.0
            cost_val = parse_number(valuation.get("cost"), default=None)

            raw_txs = scheme.get("transactions") or []
            if cost_val is None and raw_txs:
                cost_val = _estimate_cost_from_transactions(raw_txs)

            invested_value = round(float(cost_val), 2) if cost_val is not None else 0.0
            avg_nav = round(invested_value / units, 4) if units > 1e-6 and invested_value > 0 else 0.0

            if units > 1e-6:
                extra_data: dict[str, Any] = {
                    "valuation": round(val_value, 2),
                    "current_nav": round(val_nav, 4),
                    "valuation_date": as_of_date,
                    "registrar": str(scheme.get("rta") or "").strip(),
                    "rta_code": str(scheme.get("rta_code") or "").strip(),
                    "amc": amc,
                }
                if scheme.get("advisor"):
                    extra_data["advisor"] = str(scheme["advisor"]).strip()
                if scheme.get("type"):
                    extra_data["scheme_type"] = str(scheme["type"]).strip()
                if holder_name:
                    extra_data["investor_name"] = holder_name
                if pan:
                    extra_data["pan"] = pan
                if folio.get("KYC"):
                    extra_data["kyc"] = str(folio["KYC"]).strip()
                if scheme.get("nominees"):
                    extra_data["nominees"] = ", ".join(str(n) for n in scheme["nominees"])

                mf_holdings.append(
                    {
                        "folio_number": folio_no,
                        "scheme_name": scheme_name,
                        "amfi_code": amfi_code,
                        "isin": isin,
                        "units": round(float(units), 6),
                        "avg_nav": avg_nav,
                        "invested_value": invested_value,
                        "as_of_date": as_of_date,
                        "extra_data": extra_data,
                    }
                )

            for tx in raw_txs:
                tx_date = normalize_date(tx.get("date"))
                if not tx_date:
                    continue
                tx_units = abs(parse_number(tx.get("units"), default=0.0) or 0.0)
                tx_nav = abs(parse_number(tx.get("nav"), default=0.0) or 0.0)
                tx_amt = abs(parse_number(tx.get("amount"), default=0.0) or 0.0)
                desc = str(tx.get("description") or "").strip()
                side = _map_cas_tx_side(tx.get("type"), desc)
                mf_transactions.append(
                    {
                        "trade_date": tx_date,
                        "symbol": amfi_code or isin or scheme_name,
                        "scheme_name": scheme_name,
                        "folio_number": folio_no,
                        "isin": isin,
                        "amfi_code": amfi_code,
                        "asset_type": "MF",
                        "side": side,
                        "quantity": round(tx_units, 6),
                        "price": round(tx_nav, 4),
                        "amount": round(tx_amt, 2),
                        "description": desc,
                        "extra_data": {
                            "cas_type": str(tx.get("type") or ""),
                            "balance_units": parse_number(tx.get("balance"), default=0.0) or 0.0,
                            "registrar": str(scheme.get("rta") or "").strip(),
                        },
                    }
                )

    # NSDL / CDSL Depository CAS structure (if casparser detects depository CAS)
    for acct in data.get("accounts") or []:
        acct_name = str(acct.get("name") or "").strip()
        for mf in acct.get("mutual_funds") or []:
            scheme_name = re.sub(r"\s+", " ", str(mf.get("name") or "")).strip()
            if not scheme_name:
                continue
            units = parse_number(mf.get("balance"), default=0.0) or 0.0
            if units <= 1e-6:
                continue
            val_nav = parse_number(mf.get("nav"), default=0.0) or 0.0
            val_value = parse_number(mf.get("value"), default=0.0) or 0.0
            invested = parse_number(mf.get("total_cost"), default=None)
            avg_nav = parse_number(mf.get("avg_cost"), default=None)
            if invested is None and avg_nav is not None:
                invested = round(units * avg_nav, 2)
            elif avg_nav is None and invested is not None and units > 0:
                avg_nav = round(invested / units, 4)
            mf_holdings.append(
                {
                    "folio_number": str(mf.get("folio") or mf.get("ucc") or "").strip(),
                    "scheme_name": scheme_name,
                    "amfi_code": str(mf.get("amfi") or "").strip(),
                    "isin": str(mf.get("isin") or "").strip().upper(),
                    "units": round(units, 6),
                    "avg_nav": round(float(avg_nav or 0.0), 4),
                    "invested_value": round(float(invested or 0.0), 2),
                    "as_of_date": period_to,
                    "extra_data": {
                        "valuation": round(val_value, 2),
                        "current_nav": round(val_nav, 4),
                        "demat_account": acct_name,
                        "investor_name": investor_name,
                    },
                }
            )

    return {
        "mf_holdings": mf_holdings,
        "mf_transactions": mf_transactions,
        "investor_info": investor_info,
        "statement_period": statement_period,
        "warnings": warnings,
    }


def _parse_pdf_with_pdfplumber(raw_bytes: bytes, password: str = "") -> dict[str, Any]:
    """Fallback CAS/MF PDF parser using pdfplumber tables and CAS text regexes."""
    warnings: list[str] = []
    mf_holdings: list[dict[str, Any]] = []
    all_lines: list[str] = []
    table_sheets: list[list[list[Any]]] = []

    open_kwargs: dict[str, Any] = {}
    if password:
        open_kwargs["password"] = password

    with pdfplumber.open(io.BytesIO(raw_bytes), **open_kwargs) as pdf:
        combined_rows: list[list[Any]] = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.splitlines())
            for tbl in page.extract_tables() or []:
                for row in tbl:
                    if row and any(not is_cell_empty(c) for c in row):
                        combined_rows.append(
                            [c.replace("\n", " ").strip() if isinstance(c, str) else c for c in row]
                        )
        if combined_rows:
            table_sheets.append(combined_rows)

    # 1. Try tabular MF export detection first
    for sheet in table_sheets:
        detection = detect_header_and_kind(
            sheet, alias_maps={"mf_holdings": MF_HOLDINGS_COLUMN_ALIASES}, min_matches=3
        )
        if detection is not None:
            return _parse_tabular_mf_sheet(sheet, detection, warnings)

    # 2. Text-based CAS fallback
    full_text = "\n".join(all_lines)
    as_of_default = date.today().isoformat()
    period_match = re.search(
        r"To\s+(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        full_text,
        re.IGNORECASE,
    )
    if period_match:
        as_of_default = normalize_date(period_match.group(1)) or as_of_default

    investor_name = ""
    email_match = re.search(r"Email\s*Id\s*:\s*(\S+)", full_text, re.IGNORECASE)
    for line in all_lines[:20]:
        s = line.strip()
        if s and not any(
            k in s.lower()
            for k in ("consolidated", "statement", "email", "mobile", "folio", "cams", "kfintech", "page")
        ):
            if len(s) <= 60 and re.match(r"^[A-Za-z][A-Za-z\s\.]+$", s):
                investor_name = s
                break

    current_folio = ""
    current_amc = ""
    current_scheme = ""
    current_isin = ""
    current_rta = ""
    current_advisor = ""

    folio_re = re.compile(r"Folio\s*(?:No\.?|Number)?\s*:\s*([A-Za-z0-9/\-\s]+?)(?:\s{2,}|PAN|KYC|$)", re.IGNORECASE)
    isin_re = re.compile(r"\bISIN\s*:\s*(INF[A-Z0-9]{9})\b", re.IGNORECASE)
    rta_re = re.compile(r"Registrar\s*:\s*([A-Za-z0-9\s]+?)(?:\s{2,}|$)", re.IGNORECASE)
    advisor_re = re.compile(r"Advisor\s*:\s*([A-Za-z0-9\-\s]+?)(?:\s{2,}|\(|$)", re.IGNORECASE)
    close_re = re.compile(r"Closing\s+Unit\s+Balance\s*:\s*([\d,\.]+)", re.IGNORECASE)
    nav_re = re.compile(r"NAV\s+on\s+([\d\-A-Za-z]+)\s*:\s*(?:INR|Rs\.?)?\s*([\d,\.]+)", re.IGNORECASE)
    cost_re = re.compile(r"(?:Total\s+)?Cost\s+Value\s*:\s*(?:INR|Rs\.?)?\s*([\d,\.]+)", re.IGNORECASE)
    val_re = re.compile(r"(?:Market\s+Value|Valuation)\s+on\s+([\d\-A-Za-z]+)\s*:\s*(?:INR|Rs\.?)?\s*([\d,\.]+)", re.IGNORECASE)

    pending: dict[str, Any] = {}

    def _flush_pending() -> None:
        if not pending or not pending.get("scheme_name"):
            return
        units = float(pending.get("units") or 0.0)
        if units <= 1e-6:
            return
        invested = float(pending.get("invested_value") or 0.0)
        avg_nav = round(invested / units, 4) if invested > 0 and units > 0 else 0.0
        extra: dict[str, Any] = {}
        for k in ("valuation", "current_nav", "valuation_date", "registrar", "advisor", "amc", "investor_name"):
            if pending.get(k) not in (None, ""):
                extra[k] = pending[k]
        mf_holdings.append(
            {
                "folio_number": pending.get("folio_number", ""),
                "scheme_name": pending["scheme_name"],
                "amfi_code": pending.get("amfi_code", ""),
                "isin": pending.get("isin", ""),
                "units": round(units, 6),
                "avg_nav": avg_nav,
                "invested_value": round(invested, 2),
                "as_of_date": pending.get("as_of_date") or as_of_default,
                "extra_data": extra,
            }
        )

    for line in all_lines:
        stripped = line.strip()
        if not stripped:
            continue

        f_match = folio_re.search(stripped)
        if f_match:
            current_folio = f_match.group(1).strip()

        if "mutual fund" in stripped.lower() and len(stripped) < 80 and "isin" not in stripped.lower():
            current_amc = stripped

        i_match = isin_re.search(stripped)
        if i_match:
            _flush_pending()
            current_isin = i_match.group(1).upper()
            scheme_part = stripped[: i_match.start()].strip(" -–:")
            scheme_part = re.sub(r"^[A-Z0-9]+\s*-\s*", "", scheme_part).strip()
            current_scheme = re.sub(r"\s+", " ", scheme_part)
            r_match = rta_re.search(stripped)
            if r_match:
                current_rta = r_match.group(1).strip()
            a_match = advisor_re.search(stripped)
            if a_match:
                current_advisor = a_match.group(1).strip()
            pending = {
                "folio_number": current_folio,
                "scheme_name": current_scheme or current_isin,
                "isin": current_isin,
                "registrar": current_rta,
                "advisor": current_advisor,
                "amc": current_amc,
                "investor_name": investor_name,
            }
            continue

        if not pending:
            continue

        if not pending.get("registrar"):
            r_match = rta_re.search(stripped)
            if r_match:
                pending["registrar"] = r_match.group(1).strip()
        if not pending.get("advisor"):
            a_match = advisor_re.search(stripped)
            if a_match:
                pending["advisor"] = a_match.group(1).strip()

        c_match = close_re.search(stripped)
        if c_match:
            pending["units"] = parse_number(c_match.group(1), default=0.0) or 0.0

        n_match = nav_re.search(stripped)
        if n_match:
            nav_date = normalize_date(n_match.group(1))
            if nav_date:
                pending["as_of_date"] = nav_date
                pending["valuation_date"] = nav_date
            pending["current_nav"] = parse_number(n_match.group(2), default=0.0) or 0.0

        cost_m = cost_re.search(stripped)
        if cost_m:
            pending["invested_value"] = parse_number(cost_m.group(1), default=0.0) or 0.0

        val_m = val_re.search(stripped)
        if val_m:
            val_date = normalize_date(val_m.group(1))
            if val_date:
                pending["as_of_date"] = val_date
                pending["valuation_date"] = val_date
            pending["valuation"] = parse_number(val_m.group(2), default=0.0) or 0.0

    _flush_pending()

    return {
        "mf_holdings": mf_holdings,
        "mf_transactions": [],
        "investor_info": {
            "name": investor_name,
            "email": email_match.group(1) if email_match else "",
        },
        "statement_period": {"to": as_of_default},
        "warnings": warnings,
    }


def _parse_tabular_mf_sheet(
    sheet: list[list[Any]],
    detection: tuple[str, int, list[str], dict[int, str], dict[int, str]],
    warnings: list[str],
) -> dict[str, Any]:
    kind_name, header_idx, _headers, mapped_cols, unmapped_cols = detection
    field_to_col = {field: col_idx for col_idx, field in mapped_cols.items()}
    meta_rows = sheet[:header_idx]
    data_rows = sheet[header_idx + 1 :]
    meta_as_of = extract_metadata_date(meta_rows) or date.today().isoformat()

    def _cell(row: list[Any], field: str) -> Any:
        idx = field_to_col.get(field)
        return row[idx] if idx is not None and idx < len(row) else None

    if kind_name == "mf_transactions":
        mf_transactions: list[dict[str, Any]] = []
        for idx, row in enumerate(data_rows, start=1):
            if is_summary_or_total_row(row):
                continue
            scheme_name = normalize_header_text(_cell(row, "scheme_name"))
            tx_date = normalize_date(_cell(row, "trade_date"))
            if not scheme_name or not tx_date:
                continue
            units = abs(parse_number(_cell(row, "units"), default=0.0) or 0.0)
            nav = abs(parse_number(_cell(row, "nav"), default=0.0) or 0.0)
            amount = abs(parse_number(_cell(row, "amount"), default=0.0) or 0.0)
            raw_side = normalize_header_text(_cell(row, "side"))
            side = normalize_side(raw_side) or "BUY"
            amfi = normalize_header_text(_cell(row, "amfi_code"))
            isin = normalize_header_text(_cell(row, "isin")).upper()
            folio = normalize_header_text(_cell(row, "folio_number"))
            mf_transactions.append(
                {
                    "trade_date": tx_date,
                    "symbol": amfi or isin or scheme_name,
                    "scheme_name": scheme_name,
                    "folio_number": folio,
                    "isin": isin,
                    "amfi_code": amfi,
                    "asset_type": "MF",
                    "side": side,
                    "quantity": round(units, 6),
                    "price": round(nav, 4),
                    "amount": round(amount or units * nav, 2),
                    "description": raw_side,
                    "extra_data": build_extra_data(row, unmapped_cols),
                }
            )
        return {
            "mf_holdings": [],
            "mf_transactions": mf_transactions,
            "investor_info": {},
            "statement_period": {"to": meta_as_of},
            "warnings": warnings,
        }

    mf_holdings: list[dict[str, Any]] = []
    for idx, row in enumerate(data_rows, start=1):
        if is_summary_or_total_row(row):
            continue
        scheme_name = normalize_header_text(_cell(row, "scheme_name"))
        if not scheme_name:
            continue
        units = parse_number(_cell(row, "units"), default=None)
        if units is None:
            warnings.append(f"Row {idx} ({scheme_name}): missing units.")
            continue
        if units <= 1e-6:
            continue

        avg_nav = parse_number(_cell(row, "avg_nav"), default=None)
        invested = parse_number(_cell(row, "invested_value"), default=None)
        if avg_nav is None and invested is not None and units > 0:
            avg_nav = round(invested / units, 4)
        elif invested is None and avg_nav is not None:
            invested = round(units * avg_nav, 2)

        as_of = normalize_date(_cell(row, "as_of_date")) or meta_as_of
        mf_holdings.append(
            {
                "folio_number": normalize_header_text(_cell(row, "folio_number")),
                "scheme_name": scheme_name,
                "amfi_code": normalize_header_text(_cell(row, "amfi_code")),
                "isin": normalize_header_text(_cell(row, "isin")).upper(),
                "units": round(float(units), 6),
                "avg_nav": round(float(avg_nav or 0.0), 4),
                "invested_value": round(float(invested or 0.0), 2),
                "as_of_date": as_of,
                "extra_data": build_extra_data(row, unmapped_cols),
            }
        )

    return {
        "mf_holdings": mf_holdings,
        "mf_transactions": [],
        "investor_info": {},
        "statement_period": {"to": meta_as_of},
        "warnings": warnings,
    }


def parse_cams_cas(
    file_path_or_bytes: FileInput,
    filename: str,
    password: str = "",
) -> dict[str, Any]:
    """Parse a CAMS/KFintech CAS PDF or CAMS Excel/CSV mutual fund export."""
    raw_bytes, _ = read_input_bytes(file_path_or_bytes)
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        cas_error: Exception | None = None
        try:
            cas_dict = casparser.read_cas_pdf(
                io.BytesIO(raw_bytes),
                password=password,
                output="dict",
            )
            parsed = _parse_casparser_dict(cas_dict)
            if parsed["mf_holdings"] or parsed["mf_transactions"]:
                return parsed
        except Exception as exc:
            cas_error = exc

        fallback = _parse_pdf_with_pdfplumber(raw_bytes, password=password)
        if cas_error is not None:
            fallback["warnings"].append(
                f"casparser failed ({type(cas_error).__name__}: {cas_error}); used pdfplumber fallback."
            )
        if not fallback["mf_holdings"] and not fallback["mf_transactions"] and cas_error is not None:
            raise ValueError(f"Failed to parse CAS PDF '{filename}': {cas_error}") from cas_error
        return fallback

    sheets = load_tabular_sheets(raw_bytes, filename)
    if not sheets:
        raise ValueError(f"No readable tabular data found in '{filename}'.")

    warnings: list[str] = []
    alias_maps = {
        "mf_holdings": MF_HOLDINGS_COLUMN_ALIASES,
        "mf_transactions": MF_TX_COLUMN_ALIASES,
    }
    for sheet in sheets:
        detection = detect_header_and_kind(sheet, alias_maps=alias_maps, min_matches=2)
        if detection is not None:
            return _parse_tabular_mf_sheet(sheet, detection, warnings)

    raise ValueError(f"Could not detect mutual fund holdings/transactions columns in '{filename}'.")


def import_cams_cas(
    file_path_or_bytes: FileInput,
    filename: str,
    owner: str,
    password: str = "",
    replace_mf: bool = True,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Parse a CAMS/KFintech CAS PDF or export and upsert mutual fund holdings in SQLite."""
    init_db(db_path=db_path, seed_default_owners=False)
    parsed = parse_cams_cas(file_path_or_bytes, filename=filename, password=password)
    account = get_or_create_account(owner=owner, broker="cams_cas", db_path=db_path)

    upsert_res = upsert_mf_holdings(
        account_id=account["id"],
        mf_holdings=parsed["mf_holdings"],
        replace_account_mf=replace_mf,
        source="cams_cas",
        filename=filename,
        db_path=db_path,
    )

    tx_res = {"inserted": 0, "skipped": 0}
    if parsed["mf_transactions"]:
        mf_txs = [
            {"exchange": "AMFI", **tx}
            for tx in parsed["mf_transactions"]
        ]
        tx_res = insert_transactions(
            account_id=account["id"],
            transactions=mf_txs,
            source="cams_cas",
            filename=filename,
            db_path=db_path,
        )

    return {
        "account": account,
        "mf_holdings_count": len(parsed["mf_holdings"]),
        "mf_transactions_count": len(parsed["mf_transactions"]),
        "transactions_inserted": tx_res["inserted"],
        "transactions_skipped": tx_res["skipped"],
        "investor_info": parsed["investor_info"],
        "statement_period": parsed["statement_period"],
        "warnings": parsed["warnings"],
        **upsert_res,
    }
