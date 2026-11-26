"""Shared parsing, normalization, and column-matching helpers for ingestion modules."""

from __future__ import annotations

import csv
import difflib
import io
import math
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

import openpyxl
import pandas as pd


FileInput = str | Path | bytes | bytearray | BinaryIO

_SIDE_MAP: dict[str, str] = {
    "B": "BUY",
    "BUY": "BUY",
    "BOUGHT": "BUY",
    "PURCHASE": "BUY",
    "PURCHASE_SIP": "BUY",
    "SIP": "BUY",
    "SWITCH_IN": "BUY",
    "STP_IN": "BUY",
    "LONG": "BUY",
    "ACQ": "BUY",
    "ACQUISITION": "BUY",
    "S": "SELL",
    "SELL": "SELL",
    "SOLD": "SELL",
    "SALE": "SELL",
    "SHORT": "SELL",
    "REDEMPTION": "SELL",
    "REDEEM": "SELL",
    "SWITCH_OUT": "SELL",
    "STP_OUT": "SELL",
    "DIV": "DIVIDEND",
    "DIVIDEND": "DIVIDEND",
    "DIVIDEND_PAYOUT": "DIVIDEND",
    "DIVIDEND_REINVEST": "DIVIDEND",
    "BONUS": "BONUS",
    "SPLIT": "SPLIT",
    "STOCK SPLIT": "SPLIT",
    "STOCK_SPLIT": "SPLIT",
}

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d/%b/%Y",
    "%d-%B-%Y",
    "%d %B %Y",
    "%d-%m-%y",
    "%d/%m/%y",
    "%d-%b-%y",
    "%d %b %y",
    "%b %d, %Y",
    "%Y%m%d",
)

_SYMBOL_SUFFIX_RE = re.compile(r"(?:\s*-\s*(?:EQ|BE|BL|BZ|SM)|(?:\.NS|\.BO))+$", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"(?:₹|rs\.?|inr|\$)", re.IGNORECASE)


def read_input_bytes(file_path_or_bytes: FileInput) -> tuple[bytes, Path | None]:
    """Read raw bytes from a file path, bytes/bytearray, or file-like object."""
    if isinstance(file_path_or_bytes, (str, Path)):
        path = Path(file_path_or_bytes)
        return path.read_bytes(), path
    if isinstance(file_path_or_bytes, (bytes, bytearray)):
        return bytes(file_path_or_bytes), None
    if hasattr(file_path_or_bytes, "read"):
        if hasattr(file_path_or_bytes, "seek"):
            try:
                file_path_or_bytes.seek(0)
            except OSError:
                pass
        raw = file_path_or_bytes.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return bytes(raw), None
    raise TypeError(
        f"Unsupported input type {type(file_path_or_bytes).__name__}; "
        "expected path, bytes, or binary stream."
    )


def normalize_header_text(val: Any) -> str:
    """Normalize a cell value into a clean lowercase string for header matching."""
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    text = str(val).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _alnum_norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean_symbol(raw: Any) -> str:
    """Normalize an equity ticker symbol (uppercase, strip -EQ/-BE/.NS/.BO)."""
    if raw is None:
        return ""
    if isinstance(raw, float) and math.isnan(raw):
        return ""
    sym = re.sub(r"\s+", " ", str(raw)).strip().upper()
    if not sym or sym in {"NAN", "NONE", "NULL"}:
        return ""
    # Strip trailing exchange/series markers iteratively in case of e.g. RELIANCE-EQ.NS
    cleaned = _SYMBOL_SUFFIX_RE.sub("", sym).strip()
    return cleaned or sym


def normalize_side(raw: Any) -> str | None:
    """Normalize transaction side into BUY, SELL, DIVIDEND, BONUS, or SPLIT."""
    if raw is None:
        return None
    if isinstance(raw, float) and math.isnan(raw):
        return None
    token = re.sub(r"\s+", " ", str(raw)).strip().upper()
    if not token:
        return None
    if token in _SIDE_MAP:
        return _SIDE_MAP[token]
    # Handle strings like "BUY - NSE" or "PURCHASE (SIP)"
    for key, canonical in _SIDE_MAP.items():
        if len(key) >= 3 and re.search(rf"\b{re.escape(key)}\b", token):
            return canonical
    return None


def normalize_date(val: Any) -> str | None:
    """Normalize dates (strings, datetime, date, pandas Timestamp) to YYYY-MM-DD."""
    if val is None:
        return None
    if isinstance(val, float):
        if math.isnan(val):
            return None
        # Handle Excel serial dates if numeric
        if 20000 <= val <= 80000:
            try:
                ts = pd.to_datetime(val, unit="D", origin="1899-12-30")
                return ts.date().isoformat()
            except Exception:
                return None
        return None
    if isinstance(val, pd.Timestamp):
        if pd.isna(val):
            return None
        return val.date().isoformat()
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()

    text = str(val).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "-", "--"}:
        return None

    # Strip trailing time portion if present (e.g. "2026-11-20 14:30:00" or "20-11-2026T09:15:00")
    iso_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s].*)?$", text)
    if iso_match:
        y, m, d = map(int, iso_match.groups())
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None

    # Strip time component for non-ISO dates like "20-11-2026 10:15:30 AM"
    date_part = re.split(r"[T\s]+(?=\d{1,2}:\d{2})", text)[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_part, fmt).date().isoformat()
        except ValueError:
            continue

    try:
        parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.date().isoformat()
    except Exception:
        pass
    return None


def parse_number(val: Any, default: float | None = 0.0) -> float | None:
    """Parse numeric values with currency symbols, commas, or accounting parentheses."""
    if val is None:
        return default
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float, Decimal)):
        fval = float(val)
        return default if math.isnan(fval) or math.isinf(fval) else fval

    text = str(val).strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "--", "na", "n/a"}:
        return default

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _CURRENCY_RE.sub("", text).replace(",", "").replace(" ", "")
    if text.endswith("-"):
        negative = True
        text = text[:-1]

    try:
        num = float(text)
    except ValueError:
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return -abs(num) if negative else num


def is_cell_empty(val: Any) -> bool:
    """Return True if a tabular cell is effectively empty."""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    text = str(val).strip()
    return text == "" or text.lower() in {"nan", "none", "null"}


def is_summary_or_total_row(cells: list[Any]) -> bool:
    """Detect summary/footer rows such as 'Total', 'Grand Total', or disclaimers."""
    non_empty = [normalize_header_text(c) for c in cells if not is_cell_empty(c)]
    if not non_empty:
        return True
    first = non_empty[0].lower()
    if first in {"total", "grand total", "sub total", "subtotal", "net total"}:
        return True
    if re.match(r"^(?:grand\s+|sub\s*)?total\b", first):
        return True
    if len(non_empty) == 1 and len(non_empty[0]) > 60:
        return True
    return False


def match_columns_to_aliases(
    headers: list[str],
    alias_map: dict[str, list[str]],
) -> tuple[dict[int, str], dict[int, str]]:
    """Match table headers to canonical field names using exact, normalized, and fuzzy matching.

    Returns (col_idx -> canonical_field, col_idx -> original_header_name_for_unmapped).
    """
    candidates: list[tuple[float, int, str]] = []
    norm_headers = [normalize_header_text(h) for h in headers]

    for col_idx, raw_h in enumerate(norm_headers):
        if not raw_h or raw_h.lower().startswith("unnamed:"):
            continue
        raw_lower = raw_h.lower().rstrip(":*")
        alnum_h = _alnum_norm(raw_h)
        if not alnum_h:
            continue

        for field, aliases in alias_map.items():
            best_for_pair = 0.0
            for alias_idx, alias in enumerate(aliases):
                alias_lower = alias.lower().strip()
                alias_alnum = _alnum_norm(alias)
                tie_break = alias_idx * 0.05

                if raw_lower == alias_lower:
                    score = 100.0 - tie_break
                elif alnum_h == alias_alnum:
                    score = 95.0 - tie_break
                elif len(alias_alnum) >= 3 and re.search(rf"\b{re.escape(alias_alnum)}\b", alnum_h):
                    score = 84.0 - tie_break - len(alnum_h) * 0.05
                elif len(alnum_h) >= 3 and re.search(rf"\b{re.escape(alnum_h)}\b", alias_alnum):
                    score = 78.0 - tie_break
                else:
                    ratio = difflib.SequenceMatcher(None, alnum_h, alias_alnum).ratio()
                    if ratio >= 0.84:
                        score = 72.0 * ratio - tie_break
                    else:
                        score = 0.0

                if score > best_for_pair:
                    best_for_pair = score

            if best_for_pair > 0:
                candidates.append((best_for_pair, col_idx, field))

    # Greedy assignment: highest-scoring (column, field) pairs win
    candidates.sort(key=lambda item: item[0], reverse=True)
    mapped: dict[int, str] = {}
    used_fields: set[str] = set()
    for _score, col_idx, field in candidates:
        if col_idx in mapped or field in used_fields:
            continue
        mapped[col_idx] = field
        used_fields.add(field)

    unmapped: dict[int, str] = {}
    for col_idx, raw_h in enumerate(norm_headers):
        if col_idx not in mapped and raw_h and not raw_h.lower().startswith("unnamed:"):
            unmapped[col_idx] = raw_h

    return mapped, unmapped


def detect_header_and_kind(
    raw_rows: list[list[Any]],
    alias_maps: dict[str, dict[str, list[str]]],
    min_matches: int = 2,
    max_scan: int = 45,
) -> tuple[str, int, list[str], dict[int, str], dict[int, str]] | None:
    """Scan top rows of a sheet to detect header row index and table kind."""
    best: tuple[float, str, int, list[str], dict[int, str], dict[int, str]] | None = None
    scan_limit = min(len(raw_rows), max_scan)

    for row_idx in range(scan_limit):
        row = raw_rows[row_idx]
        headers = [normalize_header_text(c) for c in row]
        non_empty_count = sum(1 for h in headers if h)
        if non_empty_count < min_matches:
            continue

        for kind_name, alias_map in alias_maps.items():
            mapped, unmapped = match_columns_to_aliases(headers, alias_map)
            if len(mapped) < min_matches:
                continue

            fields = set(mapped.values())
            score = float(len(mapped))
            # Prefer tables that match core distinguishing fields
            if kind_name == "transactions":
                if "trade_date" in fields:
                    score += 1.5
                if "side" in fields:
                    score += 1.5
                if "symbol" in fields:
                    score += 1.0
                if "quantity" in fields:
                    score += 1.0
            elif kind_name == "holdings":
                if "avg_price" in fields or "invested_value" in fields:
                    score += 1.5
                if "symbol" in fields:
                    score += 1.0
                if "quantity" in fields:
                    score += 1.0
            elif kind_name == "mf_holdings":
                if "scheme_name" in fields:
                    score += 1.5
                if "units" in fields:
                    score += 1.5
                if "folio_number" in fields:
                    score += 1.0

            if best is None or score > best[0]:
                best = (score, kind_name, row_idx, headers, mapped, unmapped)

    if best is None:
        return None
    _, kind_name, row_idx, headers, mapped, unmapped = best
    return kind_name, row_idx, headers, mapped, unmapped


def extract_metadata_date(raw_rows: list[list[Any]]) -> str | None:
    """Extract an 'as of' / 'as on' / 'statement date' from metadata rows above a table."""
    date_pattern = re.compile(
        r"(?:as\s+(?:on|of)|statement\s+date|date)\s*[:\-]?\s*"
        r"(\d{1,4}[-/.\s](?:\d{1,2}|[A-Za-z]{3,9})[-/.\s]\d{2,4})",
        re.IGNORECASE,
    )
    for row in raw_rows:
        line = " ".join(normalize_header_text(c) for c in row if not is_cell_empty(c))
        if not line:
            continue
        match = date_pattern.search(line)
        if match:
            parsed = normalize_date(match.group(1))
            if parsed:
                return parsed
    return None


def _decode_csv_text(raw_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def parse_csv_rows(raw_bytes: bytes) -> list[list[Any]]:
    """Parse CSV/TSV bytes into a 2D list of strings, handling ragged metadata headers."""
    text = _decode_csv_text(raw_bytes)
    sample = "\n".join(text.splitlines()[:25])
    delimiter = ","
    if "\t" in sample and sample.count("\t") > sample.count(","):
        delimiter = "\t"
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader]


def load_tabular_sheets(raw_bytes: bytes, filename: str) -> list[list[list[Any]]]:
    """Load CSV, XLSX, or XLS bytes into a list of sheets (each a 2D list of cells)."""
    ext = Path(filename).suffix.lower()

    if ext in {".csv", ".tsv", ".txt"}:
        return [parse_csv_rows(raw_bytes)]

    if ext == ".xlsx":
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
        try:
            sheets: list[list[list[Any]]] = []
            for name in wb.sheetnames:
                ws = wb[name]
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                if rows:
                    sheets.append(rows)
            return sheets
        finally:
            wb.close()

    if ext == ".xls":
        # 1. Try xlrd if installed
        try:
            excel = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=None, header=None, engine="xlrd")
            return [df.values.tolist() for df in excel.values() if not df.empty]
        except Exception:
            pass

        # 2. Some brokers name .xlsx files with .xls extension
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
            try:
                sheets = []
                for name in wb.sheetnames:
                    rows = [list(r) for r in wb[name].iter_rows(values_only=True)]
                    if rows:
                        sheets.append(rows)
                if sheets:
                    return sheets
            finally:
                wb.close()
        except Exception:
            pass

        # 3. Many Indian brokers (including 5paisa) export HTML tables with .xls extension
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(_decode_csv_text(raw_bytes), "lxml")
            tables = soup.find_all("table")
            if tables:
                sheets = []
                for tbl in tables:
                    rows = []
                    for tr in tbl.find_all("tr"):
                        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                        if cells:
                            rows.append(cells)
                    if rows:
                        sheets.append(rows)
                if sheets:
                    return sheets
        except Exception:
            pass

        try:
            tables = pd.read_html(io.BytesIO(raw_bytes))
            if tables:
                sheets = []
                for df in tables:
                    if isinstance(df.columns, pd.MultiIndex):
                        header_rows = [
                            ["" if str(v).startswith("Unnamed:") else str(v) for v in lvl]
                            for lvl in zip(*df.columns.tolist())
                        ]
                    else:
                        header_rows = [[str(c) for c in df.columns.tolist()]]
                    body = df.values.tolist()
                    sheets.append(header_rows + body)
                return sheets
        except Exception:
            pass

        # 4. Fallback to tab/comma-delimited text disguised as .xls
        text_rows = parse_csv_rows(raw_bytes)
        if any(len(r) > 1 for r in text_rows):
            return [text_rows]

        raise ValueError(
            f"Unable to parse legacy .xls file '{filename}'. "
            "Install 'xlrd' or re-save the file as .xlsx or .csv."
        )

    raise ValueError(f"Unsupported tabular file extension '{ext}' for '{filename}'.")


def build_extra_data(row: list[Any], unmapped_cols: dict[int, str]) -> dict[str, Any]:
    """Collect unrecognized column values from a row into an extra_data dict."""
    extra: dict[str, Any] = {}
    for col_idx, col_name in unmapped_cols.items():
        if col_idx >= len(row):
            continue
        val = row[col_idx]
        if is_cell_empty(val):
            continue
        if isinstance(val, (datetime, date)):
            extra[col_name] = val.isoformat()
        elif isinstance(val, (int, float, bool)):
            extra[col_name] = val
        else:
            extra[col_name] = normalize_header_text(val)
    return extra
