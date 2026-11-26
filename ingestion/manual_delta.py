"""Quick manual trade entry parser (shorthand lines & pasted CSV/TSV) and SQLite importer."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from pathlib import Path
from typing import Any

from config import TRADEBOOK_COLUMN_ALIASES
from core.db import (
    get_or_create_account,
    init_db,
    insert_transactions,
    rebuild_holdings_from_transactions,
)
from ingestion._utils import (
    clean_symbol,
    detect_header_and_kind,
    normalize_date,
    normalize_side,
    parse_csv_rows,
    parse_number,
)
from ingestion.fivepaisa import parse_fivepaisa_file


_EXCHANGE_TOKENS = {"NSE", "BSE", "NSE_CASH", "BSE_CASH", "NFO", "MCX"}
_FILLER_TOKENS = {"ON", "OF", "AT", "FOR", "SHARES", "UNITS", "INR", "RS", "RS."}
_ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$", re.IGNORECASE)
_KV_RE = re.compile(r"^(fees?|brokerage|charges|exch|exchange|isin|note|notes)[:=](.+)$", re.IGNORECASE)
_DATE_LIKE_RE = re.compile(
    r"^(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}|\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{2,4})$"
)


def _split_manual_line(line: str) -> list[str]:
    """Tokenize a single manual entry line (tab/comma separated or whitespace shorthand)."""
    if "\t" in line:
        raw_parts = [p.strip() for p in line.split("\t") if p.strip()]
    elif "," in line and not re.search(r"\d,\d{3}(?!\d)", line):
        raw_parts = [p.strip() for p in next(csv.reader([line])) if p.strip()]
    elif line.count(",") >= 3:
        raw_parts = [p.strip() for p in next(csv.reader([line])) if p.strip()]
    else:
        normalized = re.sub(r"\s*@\s*", " @ ", line)
        raw_parts = normalized.split()

    tokens: list[str] = []
    for part in raw_parts:
        # Handle tokens like "10@2950" inside CSV cells too
        if "@" in part and part != "@":
            sub = re.sub(r"\s*@\s*", " @ ", part).split()
            tokens.extend(sub)
        else:
            tokens.append(part)
    return tokens


def _parse_single_line(line: str, line_no: int) -> dict[str, Any]:
    tokens = _split_manual_line(line)
    if not tokens:
        raise ValueError(f"Line {line_no}: empty line.")

    trade_date: str | None = None
    side: str | None = None
    symbol: str = ""
    exchange: str = "NSE"
    isin: str = ""
    fees: float = 0.0
    extra_data: dict[str, Any] = {}

    nums: list[float] = []
    at_price_idx: int | None = None
    expect_price_next = False
    symbol_candidates: list[str] = []

    for tok in tokens:
        tok_clean = tok.strip()
        if not tok_clean:
            continue
        if tok_clean == "@":
            expect_price_next = True
            continue

        upper = tok_clean.upper()
        if upper in _FILLER_TOKENS:
            continue

        kv_match = _KV_RE.match(tok_clean)
        if kv_match:
            k, v = kv_match.group(1).lower(), kv_match.group(2).strip()
            if k in {"fee", "fees", "brokerage", "charges"}:
                fees = abs(parse_number(v, default=0.0) or 0.0)
            elif k in {"exch", "exchange"}:
                exchange = "NSE" if v.upper().startswith("NSE") else "BSE" if v.upper().startswith("BSE") else v.upper()
            elif k == "isin":
                isin = v.upper()
            else:
                extra_data[k] = v
            continue

        # 1. Check date
        if trade_date is None and _DATE_LIKE_RE.match(tok_clean):
            parsed_d = normalize_date(tok_clean)
            if parsed_d:
                trade_date = parsed_d
                continue

        # 2. Check side (exact word match so symbols like DIVISLAB are safe)
        if side is None and upper in {"B", "BUY", "BOUGHT", "PURCHASE", "S", "SELL", "SOLD", "SALE", "DIV", "DIVIDEND", "BONUS", "SPLIT"}:
            side = normalize_side(upper)
            continue

        # 3. Check exchange
        if upper in _EXCHANGE_TOKENS:
            exchange = "NSE" if upper.startswith("NSE") else "BSE" if upper.startswith("BSE") else upper
            continue

        # 4. Check ISIN
        if _ISIN_RE.match(tok_clean):
            isin = upper
            continue

        # 5. Check numeric value (strip trailing '@' or leading '₹'/'Rs.')
        num_val = parse_number(tok_clean, default=None)
        if num_val is not None:
            if expect_price_next:
                at_price_idx = len(nums)
                expect_price_next = False
            nums.append(abs(float(num_val)))
            continue

        # 6. Symbol candidate
        cleaned = clean_symbol(tok_clean)
        if cleaned:
            symbol_candidates.append(cleaned)

    if symbol_candidates:
        symbol = symbol_candidates[0]
        if len(symbol_candidates) > 1:
            extra_data["note"] = " ".join(symbol_candidates[1:])

    if not symbol:
        raise ValueError(f"Line {line_no}: could not identify stock symbol in '{line}'.")
    if side is None:
        raise ValueError(f"Line {line_no}: could not identify trade side (BUY/SELL/DIVIDEND/BONUS/SPLIT) in '{line}'.")
    if not nums:
        raise ValueError(f"Line {line_no}: missing quantity/price in '{line}'.")

    trade_date = trade_date or date.today().isoformat()

    qty = 0.0
    price = 0.0
    amount = 0.0

    if at_price_idx is not None and at_price_idx < len(nums):
        price = nums[at_price_idx]
        other_nums = [n for idx, n in enumerate(nums) if idx != at_price_idx]
        qty = other_nums[0] if other_nums else (1.0 if side == "DIVIDEND" else 0.0)
        if len(other_nums) >= 2:
            amount = other_nums[1]
        else:
            amount = round(qty * price, 4)
    elif side in {"BONUS", "SPLIT"}:
        qty = nums[0]
        price = nums[1] if len(nums) >= 2 else 0.0
        amount = round(qty * price, 4)
    elif side == "DIVIDEND":
        if len(nums) == 1:
            qty = 1.0
            price = nums[0]
            amount = nums[0]
        else:
            qty = nums[0]
            price = nums[1]
            amount = nums[2] if len(nums) >= 3 else round(qty * price, 4)
    else:
        qty = nums[0]
        price = nums[1] if len(nums) >= 2 else 0.0
        amount = nums[2] if len(nums) >= 3 else round(qty * price, 4)
        if len(nums) >= 4 and fees == 0.0:
            fees = nums[3]

    if qty <= 0:
        raise ValueError(f"Line {line_no}: quantity must be positive in '{line}'.")

    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "isin": isin,
        "exchange": exchange,
        "asset_type": "EQUITY",
        "side": side,
        "quantity": round(qty, 6),
        "price": round(price, 4),
        "amount": round(amount, 4),
        "fees": round(fees, 4),
        "source": "manual",
        "extra_data": extra_data,
    }


def parse_manual_trades(text: str) -> list[dict[str, Any]]:
    """Parse pasted shorthand lines or CSV/TSV broker rows into normalized transactions.

    Supports formats such as:
    - ``2026-11-20 BUY RELIANCE 10 @ 2950``
    - ``SELL TCS 5 4100``
    - ``2026-11-20 BONUS RELIANCE 10``
    - ``2026-11-20 SPLIT TATAMOTORS 50``
    - Pasted CSV/TSV tradebook snippets (with or without headers)
    """
    if not text or not text.strip():
        return []

    raw_lines = [
        re.sub(r"(?:^|\s)#.*$", "", ln).strip()
        for ln in text.splitlines()
    ]
    non_empty = [ln for ln in raw_lines if ln]
    if not non_empty:
        return []

    # Check if the pasted block includes a CSV/TSV header matching TRADEBOOK_COLUMN_ALIASES
    clean_block = "\n".join(non_empty)
    if any(sep in clean_block for sep in (",", "\t")):
        csv_rows = parse_csv_rows(clean_block.encode("utf-8"))
        detection = detect_header_and_kind(
            csv_rows,
            alias_maps={"transactions": TRADEBOOK_COLUMN_ALIASES},
            min_matches=3,
            max_scan=10,
        )
        if detection is not None:
            parsed = parse_fivepaisa_file(
                io.BytesIO(clean_block.encode("utf-8")),
                filename="manual_paste.csv",
                kind="transactions",
            )
            for rec in parsed["records"]:
                rec["source"] = "manual"
            return parsed["records"]

    trades: list[dict[str, Any]] = []
    for idx, line in enumerate(non_empty, start=1):
        trades.append(_parse_single_line(line, line_no=idx))
    return trades


def add_manual_trades(
    text: str,
    owner: str,
    broker: str = "5paisa",
    rebuild_holdings: bool = True,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Parse manual trades and insert them into SQLite, optionally rebuilding holdings."""
    init_db(db_path=db_path, seed_default_owners=False)
    trades = parse_manual_trades(text)
    account = get_or_create_account(owner=owner, broker=broker, db_path=db_path)

    tx_res = insert_transactions(
        account_id=account["id"],
        transactions=trades,
        source="manual",
        filename="manual_entry",
        db_path=db_path,
    )

    result: dict[str, Any] = {
        "account": account,
        "parsed_count": len(trades),
        "trades": trades,
        **tx_res,
    }
    if rebuild_holdings and trades:
        result["rebuilt"] = rebuild_holdings_from_transactions(
            account_id=account["id"],
            db_path=db_path,
        )
    return result
