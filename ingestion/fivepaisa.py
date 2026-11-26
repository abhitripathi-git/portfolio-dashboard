"""5paisa & generic Indian broker tradebook/holdings parser and SQLite importer."""

from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber

from config import HOLDINGS_COLUMN_ALIASES, TRADEBOOK_COLUMN_ALIASES
from core.db import (
    get_or_create_account,
    init_db,
    insert_transactions,
    rebuild_holdings_from_transactions,
    upsert_holdings,
)
from ingestion._utils import (
    FileInput,
    build_extra_data,
    clean_symbol,
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


def _normalize_exchange(raw: Any, default: str = "NSE") -> str:
    if is_cell_empty(raw):
        return default
    val = normalize_header_text(raw).upper()
    if val in {"N", "NSE"} or val.startswith("NSE"):
        return "NSE"
    if val in {"B", "BSE"} or val.startswith("BSE"):
        return "BSE"
    return val


def _merge_split_header_tokens(tokens: list[str]) -> list[str]:
    """Rejoin multi-word column headers that were split by single-space fallback."""
    all_aliases: set[str] = set()
    for alias_map in (TRADEBOOK_COLUMN_ALIASES, HOLDINGS_COLUMN_ALIASES):
        for aliases in alias_map.values():
            for a in aliases:
                all_aliases.add(a.lower())

    merged: list[str] = []
    i = 0
    while i < len(tokens):
        cur = tokens[i]
        if i + 1 < len(tokens):
            pair = f"{cur} {tokens[i + 1]}".lower()
            if cur.lower() not in all_aliases and pair in all_aliases:
                merged.append(f"{cur} {tokens[i + 1]}")
                i += 2
                continue
        merged.append(cur)
        i += 1
    return merged


def _extract_pdf_rows(raw_bytes: bytes) -> list[list[list[Any]]]:
    """Extract candidate 2D tables from a PDF using pdfplumber tables then text fallback."""
    sheets: list[list[list[Any]]] = []

    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        # 1. Default table extraction across pages (combined into one logical sheet)
        default_rows: list[list[Any]] = []
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if row and any(not is_cell_empty(c) for c in row):
                        default_rows.append(
                            [c.replace("\n", " ").strip() if isinstance(c, str) else c for c in row]
                        )
        if default_rows and any(len(r) >= 2 for r in default_rows):
            sheets.append(default_rows)

        # 2. Text-strategy table extraction for borderless PDF statements
        text_strat_rows: list[list[Any]] = []
        for page in pdf.pages:
            tables = page.extract_tables(
                {"vertical_strategy": "text", "horizontal_strategy": "text"}
            ) or []
            for table in tables:
                for row in table:
                    if row and any(not is_cell_empty(c) for c in row):
                        text_strat_rows.append(
                            [c.replace("\n", " ").strip() if isinstance(c, str) else c for c in row]
                        )
        if text_strat_rows and any(len(r) >= 2 for r in text_strat_rows):
            sheets.append(text_strat_rows)

        # 3. Line-based text fallback (layout=True preserves column gaps in positioned PDFs)
        line_rows: list[list[Any]] = []
        for page in pdf.pages:
            text = page.extract_text(layout=True) or page.extract_text() or ""
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                parts = re.split(r"\s{2,}|\t+", stripped)
                if len(parts) == 1:
                    parts = _merge_split_header_tokens(stripped.split())
                line_rows.append(parts)
        if line_rows:
            sheets.append(line_rows)

    return sheets


def _select_alias_maps(kind: str) -> dict[str, dict[str, list[str]]]:
    norm_kind = kind.strip().lower()
    if norm_kind == "auto":
        return {
            "transactions": TRADEBOOK_COLUMN_ALIASES,
            "holdings": HOLDINGS_COLUMN_ALIASES,
        }
    if norm_kind in {"transactions", "transaction", "tradebook", "trades"}:
        return {"transactions": TRADEBOOK_COLUMN_ALIASES}
    if norm_kind in {"holdings", "holding"}:
        return {"holdings": HOLDINGS_COLUMN_ALIASES}
    raise ValueError(f"Unsupported kind '{kind}'; expected 'auto', 'transactions', or 'holdings'.")


def _row_matches_header(row: list[Any], headers: list[str]) -> bool:
    row_norm = [normalize_header_text(c).lower() for c in row]
    hdr_norm = [h.lower() for h in headers]
    if len(row_norm) < len(hdr_norm):
        return False
    non_empty_hdr = [(i, h) for i, h in enumerate(hdr_norm) if h]
    if not non_empty_hdr:
        return False
    matches = sum(1 for i, h in non_empty_hdr if row_norm[i] == h)
    return matches >= max(2, len(non_empty_hdr) - 1)


def _get_field(row: list[Any], field_to_col: dict[str, int], field: str) -> Any:
    col_idx = field_to_col.get(field)
    if col_idx is None or col_idx >= len(row):
        return None
    return row[col_idx]


def _parse_transaction_rows(
    data_rows: list[list[Any]],
    headers: list[str],
    field_to_col: dict[str, int],
    unmapped_cols: dict[int, str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row_offset, row in enumerate(data_rows, start=1):
        if is_summary_or_total_row(row) or _row_matches_header(row, headers):
            continue

        raw_date = _get_field(row, field_to_col, "trade_date")
        trade_date = normalize_date(raw_date)
        raw_symbol = _get_field(row, field_to_col, "symbol")
        symbol = clean_symbol(raw_symbol)
        if not symbol:
            if not is_cell_empty(raw_date) or not is_cell_empty(_get_field(row, field_to_col, "quantity")):
                warnings.append(f"Row {row_offset}: skipped transaction missing symbol.")
            continue
        if not trade_date:
            warnings.append(f"Row {row_offset} ({symbol}): invalid or missing trade_date '{raw_date}'.")
            continue

        extra_data = build_extra_data(row, unmapped_cols)
        raw_side = _get_field(row, field_to_col, "side")
        side = normalize_side(raw_side)

        qty = parse_number(_get_field(row, field_to_col, "quantity"), default=None)
        # Handle brokers with separate Buy Qty / Sell Qty columns if side/qty missing
        if side is None:
            buy_qty = parse_number(extra_data.get("Buy Qty") or extra_data.get("buy_qty"), default=0.0) or 0.0
            sell_qty = parse_number(extra_data.get("Sell Qty") or extra_data.get("sell_qty"), default=0.0) or 0.0
            if buy_qty > 0 and sell_qty == 0:
                side = "BUY"
                qty = buy_qty if qty is None else qty
            elif sell_qty > 0 and buy_qty == 0:
                side = "SELL"
                qty = sell_qty if qty is None else qty

        if side is None:
            warnings.append(f"Row {row_offset} ({symbol} on {trade_date}): unrecognized side '{raw_side}'.")
            continue

        if qty is None or qty == 0.0:
            if side == "DIVIDEND":
                qty = 1.0
            else:
                warnings.append(f"Row {row_offset} ({symbol} on {trade_date}): missing or zero quantity.")
                continue
        qty = abs(qty)

        price = parse_number(_get_field(row, field_to_col, "price"), default=None)
        amount = parse_number(_get_field(row, field_to_col, "amount"), default=None)
        fees = parse_number(_get_field(row, field_to_col, "fees"), default=0.0) or 0.0

        if amount is not None:
            amount = abs(amount)
        if price is not None:
            price = abs(price)

        if price is None and amount is not None and qty > 0:
            price = round(amount / qty, 4)
        elif amount is None and price is not None:
            amount = round(qty * price, 4)
        elif price is None and amount is None:
            price = 0.0
            amount = 0.0

        isin_raw = _get_field(row, field_to_col, "isin")
        isin = normalize_header_text(isin_raw).upper() if not is_cell_empty(isin_raw) else ""
        exchange = _normalize_exchange(_get_field(row, field_to_col, "exchange"))

        records.append(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "isin": isin,
                "exchange": exchange,
                "asset_type": "EQUITY",
                "side": side,
                "quantity": round(qty, 6),
                "price": round(float(price), 4),
                "amount": round(float(amount), 4),
                "fees": round(abs(float(fees)), 4),
                "extra_data": extra_data,
            }
        )
    return records


def _parse_holding_rows(
    data_rows: list[list[Any]],
    headers: list[str],
    field_to_col: dict[str, int],
    unmapped_cols: dict[int, str],
    as_of_date: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row_offset, row in enumerate(data_rows, start=1):
        if is_summary_or_total_row(row) or _row_matches_header(row, headers):
            continue

        raw_symbol = _get_field(row, field_to_col, "symbol")
        company_raw = _get_field(row, field_to_col, "company_name")
        symbol = clean_symbol(raw_symbol) or clean_symbol(company_raw)
        if not symbol:
            continue

        qty = parse_number(_get_field(row, field_to_col, "quantity"), default=None)
        if qty is None:
            warnings.append(f"Row {row_offset} ({symbol}): missing quantity.")
            continue

        avg_price = parse_number(_get_field(row, field_to_col, "avg_price"), default=None)
        invested = parse_number(_get_field(row, field_to_col, "invested_value"), default=None)

        if avg_price is None and invested is not None and qty > 0:
            avg_price = round(invested / qty, 4)
        elif invested is None and avg_price is not None:
            invested = round(qty * avg_price, 2)
        elif avg_price is None and invested is None:
            avg_price = 0.0
            invested = 0.0

        isin_raw = _get_field(row, field_to_col, "isin")
        isin = normalize_header_text(isin_raw).upper() if not is_cell_empty(isin_raw) else ""
        company_name = normalize_header_text(company_raw) if not is_cell_empty(company_raw) else ""
        exchange = _normalize_exchange(_get_field(row, field_to_col, "exchange"))
        extra_data = build_extra_data(row, unmapped_cols)

        records.append(
            {
                "symbol": symbol,
                "isin": isin,
                "company_name": company_name,
                "exchange": exchange,
                "asset_type": "EQUITY",
                "quantity": round(float(qty), 6),
                "avg_price": round(float(avg_price), 4),
                "invested_value": round(float(invested), 2),
                "as_of_date": as_of_date,
                "extra_data": extra_data,
            }
        )
    return records


def parse_fivepaisa_file(
    file_path_or_bytes: FileInput,
    filename: str,
    kind: str = "auto",
) -> dict[str, Any]:
    """Parse a 5paisa (or generic broker) CSV, Excel, or PDF tradebook/holdings file.

    Auto-detects the header row (skipping broker metadata rows above the table)
    and auto-detects ``transactions`` vs ``holdings`` when ``kind == "auto"``.
    Unrecognized columns are preserved in each record's ``extra_data`` dict.
    """
    raw_bytes, _ = read_input_bytes(file_path_or_bytes)
    ext = Path(filename).suffix.lower()
    alias_maps = _select_alias_maps(kind)

    if ext == ".pdf":
        sheets = _extract_pdf_rows(raw_bytes)
    else:
        sheets = load_tabular_sheets(raw_bytes, filename)

    if not sheets:
        raise ValueError(f"No readable tabular data found in '{filename}'.")

    detection = None
    chosen_sheet: list[list[Any]] = []
    for sheet in sheets:
        cand = detect_header_and_kind(sheet, alias_maps=alias_maps, min_matches=2)
        if cand is not None:
            detection = cand
            chosen_sheet = sheet
            break

    if detection is None:
        raise ValueError(
            f"Could not detect a valid header row in '{filename}' matching "
            f"{', '.join(alias_maps.keys())}."
        )

    detected_kind, header_idx, headers, mapped_cols, unmapped_cols = detection
    field_to_col = {field: col_idx for col_idx, field in mapped_cols.items()}
    meta_rows = chosen_sheet[:header_idx]
    data_rows = chosen_sheet[header_idx + 1 :]
    warnings: list[str] = []

    if detected_kind == "transactions":
        records = _parse_transaction_rows(
            data_rows=data_rows,
            headers=headers,
            field_to_col=field_to_col,
            unmapped_cols=unmapped_cols,
            warnings=warnings,
        )
    else:
        as_of_date = extract_metadata_date(meta_rows) or date.today().isoformat()
        records = _parse_holding_rows(
            data_rows=data_rows,
            headers=headers,
            field_to_col=field_to_col,
            unmapped_cols=unmapped_cols,
            as_of_date=as_of_date,
            warnings=warnings,
        )

    extra_columns = [unmapped_cols[i] for i in sorted(unmapped_cols)]
    return {
        "kind": detected_kind,
        "records": records,
        "extra_columns": extra_columns,
        "warnings": warnings,
    }


def import_fivepaisa_file(
    file_path_or_bytes: FileInput,
    filename: str,
    owner: str,
    kind: str = "auto",
    broker: str = "5paisa",
    replace_holdings: bool = False,
    rebuild_holdings: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Parse a 5paisa file and persist transactions or holdings into SQLite."""
    init_db(db_path=db_path, seed_default_owners=False)
    parsed = parse_fivepaisa_file(file_path_or_bytes, filename=filename, kind=kind)
    account = get_or_create_account(owner=owner, broker=broker, db_path=db_path)

    result: dict[str, Any] = {
        "account": account,
        "kind": parsed["kind"],
        "parsed_count": len(parsed["records"]),
        "extra_columns": parsed["extra_columns"],
        "warnings": parsed["warnings"],
    }

    if parsed["kind"] == "transactions":
        tx_res = insert_transactions(
            account_id=account["id"],
            transactions=parsed["records"],
            source=broker,
            filename=filename,
            db_path=db_path,
        )
        result.update(tx_res)
        if rebuild_holdings:
            result["rebuilt"] = rebuild_holdings_from_transactions(
                account_id=account["id"],
                db_path=db_path,
            )
    else:
        hold_res = upsert_holdings(
            account_id=account["id"],
            holdings=parsed["records"],
            replace_account_holdings=replace_holdings,
            source=broker,
            filename=filename,
            db_path=db_path,
        )
        result.update(hold_res)

    return result
