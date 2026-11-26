"""SQLite database layer with multi-user (Abhishek/Tanu) and flexible JSON extra_data support."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DATA_DIR, DB_PATH, DEFAULT_OWNERS

_INITIALIZED_DB_KEYS: set[tuple[str, bool]] = set()
_ALLOWED_MIGRATION_TABLES = frozenset({"transactions", "holdings", "mf_holdings", "holding_baselines"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with WAL mode & FK enabled, committing and closing cleanly on exit."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None, seed_default_owners: bool = True) -> Path:
    """Initialize SQLite tables and indexes if they do not exist."""
    path = Path(db_path) if db_path else DB_PATH
    cache_key = (str(path.resolve()), bool(seed_default_owners))
    if cache_key in _INITIALIZED_DB_KEYS and path.exists():
        return path
    with get_connection(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                broker TEXT NOT NULL DEFAULT '5paisa',
                account_name TEXT NOT NULL,
                account_code TEXT DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(owner, broker, account_name)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                isin TEXT DEFAULT '',
                exchange TEXT DEFAULT 'NSE',
                asset_type TEXT NOT NULL DEFAULT 'EQUITY',
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'DIVIDEND', 'BONUS', 'SPLIT')),
                quantity REAL NOT NULL,
                price REAL NOT NULL DEFAULT 0.0,
                amount REAL NOT NULL DEFAULT 0.0,
                fees REAL NOT NULL DEFAULT 0.0,
                source TEXT DEFAULT 'manual',
                region TEXT NOT NULL DEFAULT 'India',
                advisory_source TEXT NOT NULL DEFAULT 'Self',
                advisory_notes TEXT NOT NULL DEFAULT '',
                dedup_hash TEXT UNIQUE,
                extra_data TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tx_account_date
                ON transactions(account_id, trade_date);
            CREATE INDEX IF NOT EXISTS idx_tx_symbol
                ON transactions(symbol);

            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                isin TEXT DEFAULT '',
                company_name TEXT DEFAULT '',
                exchange TEXT DEFAULT 'NSE',
                asset_type TEXT NOT NULL DEFAULT 'EQUITY',
                quantity REAL NOT NULL,
                avg_price REAL NOT NULL DEFAULT 0.0,
                invested_value REAL NOT NULL DEFAULT 0.0,
                as_of_date TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                region TEXT NOT NULL DEFAULT 'India',
                advisory_source TEXT NOT NULL DEFAULT 'Self',
                advisory_notes TEXT NOT NULL DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, symbol, asset_type)
            );

            CREATE TABLE IF NOT EXISTS mf_holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                folio_number TEXT NOT NULL DEFAULT '',
                scheme_name TEXT NOT NULL,
                amfi_code TEXT DEFAULT '',
                isin TEXT DEFAULT '',
                units REAL NOT NULL,
                avg_nav REAL NOT NULL DEFAULT 0.0,
                invested_value REAL NOT NULL DEFAULT 0.0,
                as_of_date TEXT NOT NULL,
                source TEXT DEFAULT 'cams_cas',
                region TEXT NOT NULL DEFAULT 'India',
                advisory_source TEXT NOT NULL DEFAULT 'Self',
                advisory_notes TEXT NOT NULL DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, folio_number, scheme_name)
            );

            CREATE TABLE IF NOT EXISTS symbol_aliases (
                raw_symbol TEXT PRIMARY KEY,
                yahoo_symbol TEXT NOT NULL,
                isin TEXT DEFAULT '',
                company_name TEXT DEFAULT '',
                sector TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                source_type TEXT NOT NULL,
                filename TEXT DEFAULT '',
                rows_inserted INTEGER NOT NULL DEFAULT 0,
                rows_skipped INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS holding_baselines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                isin TEXT DEFAULT '',
                company_name TEXT DEFAULT '',
                exchange TEXT DEFAULT 'NSE',
                asset_type TEXT NOT NULL DEFAULT 'EQUITY',
                quantity REAL NOT NULL,
                avg_price REAL NOT NULL DEFAULT 0.0,
                invested_value REAL NOT NULL DEFAULT 0.0,
                as_of_date TEXT NOT NULL,
                source TEXT DEFAULT 'cdsl_dp',
                region TEXT NOT NULL DEFAULT 'India',
                advisory_source TEXT NOT NULL DEFAULT 'Self',
                advisory_notes TEXT NOT NULL DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, symbol, asset_type)
            );

            CREATE TABLE IF NOT EXISTS other_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL DEFAULT 'Abhishek',
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Other',
                invested_value REAL NOT NULL DEFAULT 0.0,
                current_value REAL NOT NULL DEFAULT 0.0,
                as_of_date TEXT NOT NULL,
                notes TEXT DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(owner, name)
            );

            CREATE TABLE IF NOT EXISTS cap_allocations (
                asset_key TEXT PRIMARY KEY,
                asset_class TEXT NOT NULL DEFAULT 'EQUITY',
                large_pct REAL NOT NULL DEFAULT 0.0,
                mid_pct REAL NOT NULL DEFAULT 0.0,
                small_pct REAL NOT NULL DEFAULT 0.0,
                other_pct REAL NOT NULL DEFAULT 0.0,
                updated_at TEXT NOT NULL
            );
            """
        )
        for tbl in ("transactions", "holdings", "mf_holdings", "holding_baselines"):
            if tbl not in _ALLOWED_MIGRATION_TABLES:
                raise ValueError(f"Disallowed migration table: {tbl}")
            existing_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "region" not in existing_cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN region TEXT NOT NULL DEFAULT 'India'")
            if "advisory_source" not in existing_cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN advisory_source TEXT NOT NULL DEFAULT 'Self'")
            if "advisory_notes" not in existing_cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN advisory_notes TEXT NOT NULL DEFAULT ''")
        if seed_default_owners:
            now = _utc_now()
            for owner in DEFAULT_OWNERS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts (owner, broker, account_name, created_at)
                    VALUES (?, '5paisa', ?, ?)
                    """,
                    (owner, f"{owner} - 5paisa", now),
                )
        conn.commit()
    _INITIALIZED_DB_KEYS.add(cache_key)
    return path


def _normalize_extra_data(extra_data: dict[str, Any] | str | None) -> str:
    if extra_data is None:
        return "{}"
    if isinstance(extra_data, str):
        return extra_data
    clean = {str(k): (None if v is None else str(v) if not isinstance(v, (int, float, bool)) else v)
             for k, v in extra_data.items()}
    return json.dumps(clean, ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if "extra_data" in d and isinstance(d["extra_data"], str):
        try:
            d["extra_data"] = json.loads(d["extra_data"]) if d["extra_data"] else {}
        except json.JSONDecodeError:
            d["extra_data"] = {}
    return d


def get_or_create_account(
    owner: str,
    broker: str = "5paisa",
    account_name: str | None = None,
    account_code: str = "",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Fetch an account for an owner+broker, creating it if missing."""
    clean_owner = owner.strip().title()
    clean_broker = broker.strip() or "5paisa"
    clean_name = (account_name or f"{clean_owner} - {clean_broker}").strip()
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM accounts
            WHERE LOWER(owner) = LOWER(?) AND LOWER(broker) = LOWER(?) AND LOWER(account_name) = LOWER(?)
            """,
            (clean_owner, clean_broker, clean_name),
        ).fetchone()
        if row:
            return _row_to_dict(row)
        now = _utc_now()
        cur = conn.execute(
            """
            INSERT INTO accounts (owner, broker, account_name, account_code, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_owner, clean_broker, clean_name, account_code, now),
        )
        conn.commit()
        created = conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(created)


def list_accounts(
    owner: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List accounts, optionally filtered by owner name (case-insensitive)."""
    with get_connection(db_path) as conn:
        if owner and owner.strip().lower() not in ("all", "combined", "both", "*"):
            rows = conn.execute(
                "SELECT * FROM accounts WHERE LOWER(owner) = LOWER(?) ORDER BY owner, id",
                (owner.strip(),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accounts ORDER BY owner, id").fetchall()
        return [_row_to_dict(r) for r in rows]


def _resolve_account_ids(
    conn: sqlite3.Connection,
    owner: str | None = None,
    account_id: int | None = None,
) -> list[int] | None:
    if account_id is not None:
        return [int(account_id)]
    if owner and owner.strip().lower() not in ("all", "combined", "both", "*"):
        rows = conn.execute(
            "SELECT id FROM accounts WHERE LOWER(owner) = LOWER(?)",
            (owner.strip(),),
        ).fetchall()
        return [int(r["id"]) for r in rows]
    return None


def compute_tx_dedup_hash(tx: dict[str, Any], account_id: int) -> str:
    """Create a deterministic hash so re-importing overlapping CSVs skips duplicates."""
    parts = [
        str(account_id),
        str(tx.get("trade_date", "")).strip(),
        str(tx.get("symbol", "")).strip().upper(),
        str(tx.get("side", "")).strip().upper(),
        f"{float(tx.get('quantity', 0.0)):.6f}",
        f"{float(tx.get('price', 0.0)):.4f}",
        str(tx.get("isin", "")).strip().upper(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def insert_transactions(
    account_id: int,
    transactions: list[dict[str, Any]],
    source: str = "manual",
    filename: str = "",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Insert transactions with automatic deduplication. Returns inserted/skipped counts."""
    inserted = 0
    skipped = 0
    now = _utc_now()
    with get_connection(db_path) as conn:
        for tx in transactions:
            qty = float(tx.get("quantity", 0.0))
            price = float(tx.get("price", 0.0))
            amount = float(tx.get("amount", round(qty * price, 4)))
            fees = float(tx.get("fees", 0.0))
            dedup_hash = tx.get("dedup_hash") or compute_tx_dedup_hash(tx, account_id)
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO transactions (
                        account_id, trade_date, symbol, isin, exchange, asset_type,
                        side, quantity, price, amount, fees, source,
                        region, advisory_source, advisory_notes, dedup_hash,
                        extra_data, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        str(tx["trade_date"]).strip(),
                        str(tx["symbol"]).strip().upper(),
                        str(tx.get("isin", "")).strip().upper(),
                        str(tx.get("exchange", "NSE")).strip().upper(),
                        str(tx.get("asset_type", "EQUITY")).strip().upper(),
                        str(tx["side"]).strip().upper(),
                        qty,
                        price,
                        amount,
                        fees,
                        tx.get("source", source),
                        str(tx.get("region") or "India").strip(),
                        str(tx.get("advisory_source") or "Self").strip(),
                        str(tx.get("advisory_notes") or "").strip(),
                        dedup_hash,
                        _normalize_extra_data(tx.get("extra_data")),
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except sqlite3.IntegrityError:
                skipped += 1

        conn.execute(
            """
            INSERT INTO import_logs (account_id, source_type, filename, rows_inserted, rows_skipped, imported_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (account_id, f"transactions:{source}", filename, inserted, skipped, now),
        )
        conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def _upsert_equity_table(
    table: str,
    account_id: int,
    holdings: list[dict[str, Any]],
    replace_existing: bool,
    source: str,
    db_path: Path | str | None,
    log_import_filename: str | None = None,
) -> dict[str, int]:
    """Shared upserter for `holdings` and `holding_baselines` preserving metadata."""
    now = _utc_now()
    upserted = 0
    with get_connection(db_path) as conn:
        preserved_meta: dict[tuple[str, str], tuple[str, str, str]] = {}
        if replace_existing:
            for prev_r in conn.execute(
                f"SELECT symbol, asset_type, region, advisory_source, advisory_notes FROM {table} WHERE account_id = ?",
                (account_id,),
            ).fetchall():
                preserved_meta[(str(prev_r["symbol"]).upper(), str(prev_r["asset_type"]).upper())] = (
                    str(prev_r["region"] or "India"),
                    str(prev_r["advisory_source"] or "Self"),
                    str(prev_r["advisory_notes"] or ""),
                )
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))

        for h in holdings:
            qty = float(h.get("quantity", 0.0))
            avg_price = float(h.get("avg_price", 0.0))
            invested = float(h.get("invested_value", round(qty * avg_price, 2)))
            as_of = str(h.get("as_of_date") or datetime.now().date().isoformat())
            sym_u = str(h["symbol"]).strip().upper()
            atype_u = str(h.get("asset_type", "EQUITY")).strip().upper()
            prev_reg, prev_adv, prev_notes = preserved_meta.get((sym_u, atype_u), ("India", "Self", ""))
            reg_val = str(h.get("region") or prev_reg or "India").strip()
            adv_src = str(h.get("advisory_source") or prev_adv or "Self").strip()
            adv_notes = str(h.get("advisory_notes") if h.get("advisory_notes") is not None else prev_notes).strip()
            conn.execute(
                f"""
                INSERT INTO {table} (
                    account_id, symbol, isin, company_name, exchange, asset_type,
                    quantity, avg_price, invested_value, as_of_date, source,
                    region, advisory_source, advisory_notes, extra_data, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, symbol, asset_type) DO UPDATE SET
                    isin = CASE WHEN excluded.isin != '' THEN excluded.isin ELSE {table}.isin END,
                    company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE {table}.company_name END,
                    exchange = excluded.exchange,
                    quantity = excluded.quantity,
                    avg_price = excluded.avg_price,
                    invested_value = excluded.invested_value,
                    as_of_date = excluded.as_of_date,
                    source = excluded.source,
                    region = CASE WHEN excluded.region != '' THEN excluded.region ELSE {table}.region END,
                    advisory_source = CASE WHEN excluded.advisory_source != '' THEN excluded.advisory_source ELSE {table}.advisory_source END,
                    advisory_notes = CASE WHEN excluded.advisory_notes != '' THEN excluded.advisory_notes ELSE {table}.advisory_notes END,
                    extra_data = excluded.extra_data,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    sym_u,
                    str(h.get("isin", "")).strip().upper(),
                    str(h.get("company_name", "")).strip(),
                    str(h.get("exchange", "NSE")).strip().upper(),
                    atype_u,
                    qty,
                    avg_price,
                    invested,
                    as_of,
                    h.get("source", source),
                    reg_val,
                    adv_src,
                    adv_notes,
                    _normalize_extra_data(h.get("extra_data")),
                    now,
                ),
            )
            upserted += 1

        if log_import_filename is not None:
            conn.execute(
                """
                INSERT INTO import_logs (account_id, source_type, filename, rows_inserted, rows_skipped, imported_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (account_id, f"holdings:{source}", log_import_filename, upserted, now),
            )
        conn.commit()
    return {"upserted": upserted}


def upsert_holdings(
    account_id: int,
    holdings: list[dict[str, Any]],
    replace_account_holdings: bool = False,
    source: str = "manual",
    filename: str = "",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Insert or update stock holdings for an account."""
    return _upsert_equity_table(
        table="holdings",
        account_id=account_id,
        holdings=holdings,
        replace_existing=replace_account_holdings,
        source=source,
        db_path=db_path,
        log_import_filename=filename,
    )


def upsert_mf_holdings(
    account_id: int,
    mf_holdings: list[dict[str, Any]],
    replace_account_mf: bool = False,
    source: str = "cams_cas",
    filename: str = "",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Insert or update mutual fund holdings for an account."""
    now = _utc_now()
    upserted = 0
    with get_connection(db_path) as conn:
        if replace_account_mf:
            conn.execute("DELETE FROM mf_holdings WHERE account_id = ?", (account_id,))
        for mf in mf_holdings:
            units = float(mf.get("units", 0.0))
            avg_nav = float(mf.get("avg_nav", 0.0))
            invested = float(mf.get("invested_value", round(units * avg_nav, 2)))
            as_of = str(mf.get("as_of_date") or datetime.now().date().isoformat())
            reg_val = str(mf.get("region") or "India").strip()
            adv_src = str(mf.get("advisory_source") or "Self").strip()
            adv_notes = str(mf.get("advisory_notes") or "").strip()
            conn.execute(
                """
                INSERT INTO mf_holdings (
                    account_id, folio_number, scheme_name, amfi_code, isin,
                    units, avg_nav, invested_value, as_of_date, source,
                    region, advisory_source, advisory_notes, extra_data, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, folio_number, scheme_name) DO UPDATE SET
                    amfi_code = CASE WHEN excluded.amfi_code != '' THEN excluded.amfi_code ELSE mf_holdings.amfi_code END,
                    isin = CASE WHEN excluded.isin != '' THEN excluded.isin ELSE mf_holdings.isin END,
                    units = excluded.units,
                    avg_nav = excluded.avg_nav,
                    invested_value = excluded.invested_value,
                    as_of_date = excluded.as_of_date,
                    source = excluded.source,
                    region = CASE WHEN excluded.region != '' THEN excluded.region ELSE mf_holdings.region END,
                    advisory_source = CASE WHEN excluded.advisory_source != '' THEN excluded.advisory_source ELSE mf_holdings.advisory_source END,
                    advisory_notes = CASE WHEN excluded.advisory_notes != '' THEN excluded.advisory_notes ELSE mf_holdings.advisory_notes END,
                    extra_data = excluded.extra_data,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    str(mf.get("folio_number", "")).strip(),
                    str(mf["scheme_name"]).strip(),
                    str(mf.get("amfi_code", "")).strip(),
                    str(mf.get("isin", "")).strip().upper(),
                    units,
                    avg_nav,
                    invested,
                    as_of,
                    mf.get("source", source),
                    reg_val,
                    adv_src,
                    adv_notes,
                    _normalize_extra_data(mf.get("extra_data")),
                    now,
                ),
            )
            upserted += 1

        conn.execute(
            """
            INSERT INTO import_logs (account_id, source_type, filename, rows_inserted, rows_skipped, imported_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (account_id, f"mf_holdings:{source}", filename, upserted, now),
        )
        conn.commit()
    return {"upserted": upserted}


def query_transactions(
    owner: str | None = None,
    account_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    symbol: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch transactions joined with account owner/broker metadata."""
    with get_connection(db_path) as conn:
        clauses = ["1=1"]
        params: list[Any] = []
        ids = _resolve_account_ids(conn, owner=owner, account_id=account_id)
        if ids is not None:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"t.account_id IN ({placeholders})")
            params.extend(ids)
        if start_date:
            clauses.append("t.trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("t.trade_date <= ?")
            params.append(end_date)
        if symbol:
            clauses.append("UPPER(t.symbol) = UPPER(?)")
            params.append(symbol.strip())

        sql = f"""
            SELECT t.*, a.owner, a.broker, a.account_name
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.trade_date ASC, t.id ASC
        """
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def query_holdings(
    owner: str | None = None,
    account_id: int | None = None,
    exclude_symbols: list[str] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch equity holdings joined with account owner/broker metadata."""
    with get_connection(db_path) as conn:
        clauses = ["h.quantity > 0"]
        params: list[Any] = []
        ids = _resolve_account_ids(conn, owner=owner, account_id=account_id)
        if ids is not None:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"h.account_id IN ({placeholders})")
            params.extend(ids)
        if exclude_symbols:
            clean_excl = [s.strip().upper() for s in exclude_symbols if s.strip()]
            if clean_excl:
                placeholders = ",".join("?" for _ in clean_excl)
                clauses.append(f"UPPER(h.symbol) NOT IN ({placeholders})")
                params.extend(clean_excl)

        sql = f"""
            SELECT h.*, a.owner, a.broker, a.account_name
            FROM holdings h
            JOIN accounts a ON a.id = h.account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.owner, h.symbol
        """
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def query_mf_holdings(
    owner: str | None = None,
    account_id: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch mutual fund holdings joined with account owner/broker metadata."""
    with get_connection(db_path) as conn:
        clauses = ["m.units > 0"]
        params: list[Any] = []
        ids = _resolve_account_ids(conn, owner=owner, account_id=account_id)
        if ids is not None:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"m.account_id IN ({placeholders})")
            params.extend(ids)

        sql = f"""
            SELECT m.*, a.owner, a.broker, a.account_name
            FROM mf_holdings m
            JOIN accounts a ON a.id = m.account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.owner, m.scheme_name
        """
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def upsert_holding_baselines(
    account_id: int,
    holdings: list[dict[str, Any]],
    replace_account_baselines: bool = True,
    source: str = "cdsl_dp",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Persist reconciled depository/opening holding baselines for an account."""
    return _upsert_equity_table(
        table="holding_baselines",
        account_id=account_id,
        holdings=holdings,
        replace_existing=replace_account_baselines,
        source=source,
        db_path=db_path,
        log_import_filename=None,
    )


def query_holding_baselines(
    owner: str | None = None,
    account_id: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch reconciled holding baselines joined with account metadata."""
    with get_connection(db_path) as conn:
        clauses = ["1=1"]
        params: list[Any] = []
        ids = _resolve_account_ids(conn, owner=owner, account_id=account_id)
        if ids is not None:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"b.account_id IN ({placeholders})")
            params.extend(ids)
        sql = f"""
            SELECT b.*, a.owner, a.broker, a.account_name
            FROM holding_baselines b
            JOIN accounts a ON a.id = b.account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.owner, b.symbol
        """
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def _update_holding_metadata(
    account_id: int,
    symbol_or_scheme: str,
    updates: dict[str, Any],
    asset_class: str = "EQUITY",
    include_transactions: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Shared updater for `region`, `advisory_source`, and `advisory_notes`."""
    target = symbol_or_scheme.strip()
    now = _utc_now()
    set_cols = ", ".join(f"{col} = ?" for col in updates)
    vals = list(updates.values())
    updated = 0
    with get_connection(db_path) as conn:
        if asset_class.upper() == "MF":
            cur = conn.execute(
                f"""
                UPDATE mf_holdings SET {set_cols}, updated_at = ?
                WHERE account_id = ? AND (UPPER(scheme_name) = UPPER(?) OR UPPER(isin) = UPPER(?) OR amfi_code = ?)
                """,
                (*vals, now, account_id, target, target, target),
            )
            updated += cur.rowcount
        else:
            for tbl in ("holdings", "holding_baselines"):
                cur = conn.execute(
                    f"""
                    UPDATE {tbl} SET {set_cols}, updated_at = ?
                    WHERE account_id = ? AND (UPPER(symbol) = UPPER(?) OR UPPER(isin) = UPPER(?))
                    """,
                    (*vals, now, account_id, target, target),
                )
                updated += cur.rowcount
            if include_transactions:
                cur_tx = conn.execute(
                    f"""
                    UPDATE transactions SET {set_cols}
                    WHERE account_id = ? AND (UPPER(symbol) = UPPER(?) OR UPPER(isin) = UPPER(?))
                    """,
                    (*vals, account_id, target, target),
                )
                updated += cur_tx.rowcount
        conn.commit()
    return {"updated": updated}


def update_asset_region(
    account_id: int,
    symbol_or_scheme: str,
    region: str = "Foreign",
    asset_class: str = "EQUITY",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Update region ('India' or 'Foreign') across holdings, baselines, MFs, and transactions."""
    return _update_holding_metadata(
        account_id=account_id,
        symbol_or_scheme=symbol_or_scheme,
        updates={"region": region.strip().title() or "India"},
        asset_class=asset_class,
        include_transactions=True,
        db_path=db_path,
    )


def update_advisory_source(
    account_id: int,
    symbol_or_scheme: str,
    advisory_source: str,
    advisory_notes: str = "",
    asset_class: str = "EQUITY",
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Update advisory_source/advisory_notes across holdings, baselines, or MFs for a symbol."""
    return _update_holding_metadata(
        account_id=account_id,
        symbol_or_scheme=symbol_or_scheme,
        updates={
            "advisory_source": advisory_source.strip() or "Self",
            "advisory_notes": advisory_notes.strip(),
        },
        asset_class=asset_class,
        include_transactions=False,
        db_path=db_path,
    )


def rebuild_holdings_from_transactions(
    account_id: int,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Recompute net equity holdings (FIFO/weighted avg) from baseline + transactions."""
    with get_connection(db_path) as conn:
        baseline_rows = [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM holding_baselines WHERE account_id = ?",
                (account_id,),
            ).fetchall()
        ]

    positions: dict[str, dict[str, Any]] = {}
    max_baseline_date: str | None = None
    for b in baseline_rows:
        sym = str(b["symbol"]).strip().upper()
        qty = float(b.get("quantity", 0.0))
        avg_price = float(b.get("avg_price", 0.0))
        invested = float(b.get("invested_value") or round(qty * avg_price, 2))
        as_of = str(b["as_of_date"])
        if max_baseline_date is None or as_of > max_baseline_date:
            max_baseline_date = as_of
        positions[sym] = {
            "symbol": sym,
            "isin": b.get("isin", ""),
            "company_name": b.get("company_name", ""),
            "exchange": b.get("exchange", "NSE"),
            "asset_type": b.get("asset_type", "EQUITY"),
            "quantity": qty,
            "cost_basis": invested,
            "last_date": as_of,
            "source": b.get("source", "cdsl_dp"),
            "region": b.get("region") or "India",
            "advisory_source": b.get("advisory_source") or "Self",
            "advisory_notes": b.get("advisory_notes") or "",
            "extra_data": b.get("extra_data") or {},
        }

    txs = query_transactions(account_id=account_id, db_path=db_path)
    for tx in txs:
        if str(tx.get("asset_type") or "EQUITY").strip().upper() == "MF":
            continue
        if max_baseline_date and str(tx["trade_date"]) <= max_baseline_date:
            continue
        sym = tx["symbol"]
        side = tx["side"]
        qty = float(tx["quantity"])
        price = float(tx["price"])
        pos = positions.setdefault(
            sym,
            {
                "symbol": sym,
                "isin": tx.get("isin", ""),
                "company_name": "",
                "exchange": tx.get("exchange", "NSE"),
                "asset_type": tx.get("asset_type", "EQUITY"),
                "quantity": 0.0,
                "cost_basis": 0.0,
                "last_date": tx["trade_date"],
                "source": "rebuilt_from_transactions",
                "region": tx.get("region") or "India",
                "advisory_source": tx.get("advisory_source") or "Self",
                "advisory_notes": tx.get("advisory_notes") or "",
                "extra_data": {},
            },
        )
        pos["last_date"] = max(pos["last_date"], tx["trade_date"])
        if tx.get("isin"):
            pos["isin"] = tx["isin"]
        if tx.get("region") and str(tx["region"]).strip() not in ("", "India"):
            pos["region"] = str(tx["region"]).strip()
        if tx.get("advisory_source") and str(tx["advisory_source"]).strip() not in ("", "Self"):
            pos["advisory_source"] = str(tx["advisory_source"]).strip()
        if tx.get("advisory_notes"):
            pos["advisory_notes"] = str(tx["advisory_notes"]).strip()

        if side == "BUY":
            pos["quantity"] += qty
            pos["cost_basis"] += qty * price
        elif side == "BONUS":
            # Bonus adds free shares, lowering average cost per share
            pos["quantity"] += qty
        elif side == "SPLIT":
            # Split multiplier stored in quantity if ratio > 0, or additional shares
            if qty > 0:
                pos["quantity"] += qty
        elif side == "SELL":
            if pos["quantity"] > 0:
                avg = pos["cost_basis"] / pos["quantity"]
                sell_qty = min(qty, pos["quantity"])
                pos["quantity"] -= sell_qty
                pos["cost_basis"] -= sell_qty * avg

    computed = []
    for pos in positions.values():
        if pos["quantity"] > 1e-6:
            avg_price = pos["cost_basis"] / pos["quantity"]
            computed.append(
                {
                    "symbol": pos["symbol"],
                    "isin": pos["isin"],
                    "company_name": pos.get("company_name", ""),
                    "exchange": pos["exchange"],
                    "asset_type": pos["asset_type"],
                    "quantity": round(pos["quantity"], 6),
                    "avg_price": round(avg_price, 4),
                    "invested_value": round(pos["cost_basis"], 2),
                    "as_of_date": pos["last_date"],
                    "source": pos.get("source", "rebuilt_from_transactions"),
                    "region": pos.get("region", "India"),
                    "advisory_source": pos.get("advisory_source", "Self"),
                    "advisory_notes": pos.get("advisory_notes", ""),
                    "extra_data": pos.get("extra_data", {}),
                }
            )
    target_path = Path(db_path) if db_path else DB_PATH
    if target_path.exists() and target_path.resolve() == DB_PATH.resolve():
        bak_path = DATA_DIR / "portfolio.pre_rebuild.bak"
        src_conn = sqlite3.connect(str(target_path), timeout=15.0)
        dst_conn = sqlite3.connect(str(bak_path), timeout=15.0)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
    return upsert_holdings(
        account_id=account_id,
        holdings=computed,
        replace_account_holdings=True,
        source="rebuilt_from_transactions",
        db_path=db_path,
    )


def save_symbol_alias(
    raw_symbol: str,
    yahoo_symbol: str,
    isin: str = "",
    company_name: str = "",
    sector: str = "",
    db_path: Path | str | None = None,
) -> None:
    """Persist a symbol mapping so Yahoo/NSE lookups only need resolution once."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO symbol_aliases (raw_symbol, yahoo_symbol, isin, company_name, sector, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw_symbol) DO UPDATE SET
                yahoo_symbol = excluded.yahoo_symbol,
                isin = CASE WHEN excluded.isin != '' THEN excluded.isin ELSE symbol_aliases.isin END,
                company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE symbol_aliases.company_name END,
                sector = CASE WHEN excluded.sector != '' THEN excluded.sector ELSE symbol_aliases.sector END,
                updated_at = excluded.updated_at
            """,
            (raw_symbol.strip().upper(), yahoo_symbol.strip(), isin.strip().upper(), company_name, sector, _utc_now()),
        )
        conn.commit()


def get_symbol_aliases(db_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return all saved symbol aliases keyed by raw_symbol."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM symbol_aliases").fetchall()
        return {r["raw_symbol"]: dict(r) for r in rows}


def get_import_logs(limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Fetch recent import logs."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.*, a.owner, a.account_name
            FROM import_logs l
            LEFT JOIN accounts a ON a.id = l.account_id
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def query_other_assets(
    owner: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch static/alternative assets (NPS, EPFO, PPF, Real Estate), optionally filtered by owner."""
    with get_connection(db_path) as conn:
        if owner and owner.strip().lower() not in ("all", "combined", "both", "*"):
            rows = conn.execute(
                "SELECT * FROM other_assets WHERE LOWER(owner) = LOWER(?) ORDER BY category, name",
                (owner.strip(),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM other_assets ORDER BY owner, category, name").fetchall()
        return [_row_to_dict(r) for r in rows]


def save_other_assets(
    assets: list[dict[str, Any]],
    owner: str | None = None,
    replace_scope: bool = True,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Save static/alternative assets for an owner (or combined), replacing scoped rows when requested."""
    now = _utc_now()
    today_iso = datetime.now().date().isoformat()
    clean_owner = (
        owner.strip().title()
        if owner and owner.strip().lower() not in ("all", "combined", "both", "*")
        else None
    )
    saved = 0
    with get_connection(db_path) as conn:
        if replace_scope:
            if clean_owner:
                conn.execute("DELETE FROM other_assets WHERE LOWER(owner) = LOWER(?)", (clean_owner,))
            else:
                conn.execute("DELETE FROM other_assets")

        for item in assets:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            row_owner = str(item.get("owner") or clean_owner or "Abhishek").strip().title()
            category = str(item.get("category") or "Other").strip() or "Other"
            inv = float(item.get("invested_value") or 0.0)
            curr = float(item.get("current_value") or 0.0)
            as_of = str(item.get("as_of_date") or today_iso).strip()
            notes = str(item.get("notes") or "").strip()
            conn.execute(
                """
                INSERT INTO other_assets (
                    owner, name, category, invested_value, current_value,
                    as_of_date, notes, extra_data, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, name) DO UPDATE SET
                    category = excluded.category,
                    invested_value = excluded.invested_value,
                    current_value = excluded.current_value,
                    as_of_date = excluded.as_of_date,
                    notes = excluded.notes,
                    extra_data = excluded.extra_data,
                    updated_at = excluded.updated_at
                """,
                (
                    row_owner,
                    name,
                    category,
                    round(inv, 2),
                    round(curr, 2),
                    as_of,
                    notes,
                    _normalize_extra_data(item.get("extra_data")),
                    now,
                ),
            )
            saved += 1
        conn.commit()
    return {"saved": saved}


def query_cap_allocations(db_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return cached Large/Mid/Small/Other % allocations keyed by uppercase asset_key."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM cap_allocations").fetchall()
        return {str(r["asset_key"]).strip().upper(): dict(r) for r in rows}


def upsert_cap_allocations(rows: list[dict[str, Any]], db_path: Path | str | None = None) -> int:
    """Persist point-in-time market-cap percentage allocations into SQLite."""
    now = _utc_now()
    saved = 0
    with get_connection(db_path) as conn:
        for r in rows:
            key = str(r.get("asset_key") or "").strip().upper()
            if not key:
                continue
            conn.execute(
                """
                INSERT INTO cap_allocations (
                    asset_key, asset_class, large_pct, mid_pct, small_pct, other_pct, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_key) DO UPDATE SET
                    asset_class = excluded.asset_class,
                    large_pct = excluded.large_pct,
                    mid_pct = excluded.mid_pct,
                    small_pct = excluded.small_pct,
                    other_pct = excluded.other_pct,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    str(r.get("asset_class") or "EQUITY").strip().upper(),
                    round(float(r.get("large_pct") or 0.0), 2),
                    round(float(r.get("mid_pct") or 0.0), 2),
                    round(float(r.get("small_pct") or 0.0), 2),
                    round(float(r.get("other_pct") or 0.0), 2),
                    now,
                ),
            )
            saved += 1
        conn.commit()
    return saved


def _readonly_sqlite_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger: str | None,
) -> int:
    _ = (db_name, trigger)
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        pname = (arg1 or "").strip().lower()
        if pname in {"table_info", "table_list", "index_list", "foreign_key_list"}:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_readonly_query(
    sql: str,
    db_path: Path | str | None = None,
    max_rows: int = 500,
) -> list[dict[str, Any]]:
    """Execute a read-only SELECT/WITH/schema PRAGMA query enforced by URI mode=ro and SQLite authorizer."""
    stripped = sql.strip().lstrip("(")
    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH") or upper.startswith("PRAGMA")):
        raise ValueError("Only read-only SELECT, WITH, or PRAGMA schema queries are allowed.")

    path = (Path(db_path) if db_path else DB_PATH).resolve()
    if not path.exists():
        return []

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.set_authorizer(_readonly_sqlite_authorizer)
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
