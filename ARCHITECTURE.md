# Architecture & Audit Guide (`portfolio-dashboard`)

This document explains every module, database table, financial calculation, and design invariant in `portfolio-dashboard` so any engineer or AI assistant (Claude, Cursor, Antigravity, etc.) can audit, extend, or rebuild the system confidently.

---

## 1. Directory & File Inventory

```text
portfolio-dashboard/
├── app.py                        # Streamlit entry point: init_db, CSS, sidebar, hero header, 6 tabs
├── config.py                     # Paths (DB_PATH), DEFAULT_OWNERS, BENCHMARKS, column aliases, SYMBOL_OVERRIDES
├── mcp_server.py                 # FastMCP stdio server exposing 10 portfolio/analytics/SQL tools
├── pyproject.toml                # Python >=3.11 dependencies & pytest config
├── uv.lock                       # Exact pinned dependency lockfile for reproducible rebuilds
├── start_dashboard.command       # Portable macOS launcher (self-locating + auto-bootstraps .venv)
├── .env.example                  # Template for GEMINI_API_KEY and optional PORTFOLIO_DB_PATH
├── README.md                     # User setup, rebuild, backup, and usage guide
├── ARCHITECTURE.md               # This technical & audit reference
├── CLAUDE.md                     # Quick-start instructions & invariants for AI coding assistants
├── core/
│   ├── db.py                     # SQLite schema, safe migrations, CRUD, baselines, rebuild engine, read-only SQL
│   ├── market_data.py            # On-demand yfinance & mftool (AMFI) fetchers + thread-safe in-memory TTL cache
│   ├── analytics.py              # XIRR, live snapshots, baseline-aware benchmark replay & TWR/what-if simulator
│   ├── overlap.py                # MF-vs-direct-stock & MF-vs-MF holdings overlap analyzer
│   ├── gemini_chat.py            # Hybrid M5-local digest + parallel Gemini Cloud function-calling engine
│   └── server_control.py         # Detached macOS Streamlit process restarter used by sidebar Restart App button
├── ingestion/
│   ├── _utils.py                 # Number/date/side normalization, header detection, CSV/XLSX/HTML-XLS loaders
│   ├── fivepaisa.py              # 5paisa & generic broker CSV/XLSX/PDF parser & SQLite importer
│   ├── cams_cas.py               # CAMS/KFintech CAS PDF/XLSX/CSV MF parser & importer (casparser + pdfplumber)
│   └── manual_delta.py           # Shorthand text/CSV monthly delta parser (`YYYY-MM-DD BUY SYMBOL QTY @ PRICE`)
├── ui/
│   ├── data_service.py           # Streamlit `@st.cache_data` wrappers + `get_enriched_snapshot`
│   ├── formatters.py             # Material WealthTech CSS, Indian INR formatting, extra_data expansion
│   ├── sidebar.py                # Owner/account filters, Refresh/Restart, Sync Online (Drive DB + GitHub), DB status
│   ├── tab_overview.py           # KPIs, Net Worth strip, allocation, advisory & geography breakdowns
│   ├── tab_benchmark.py          # Overall Combined + Benchmark + Segment & MF dropdowns
│   ├── tab_holdings.py           # Searchable/filterable Equity/SIF, MF, and Transactions tables + Movers chart
│   ├── tab_alt_assets.py         # Dedicated Alternative & Fixed Assets (NPS/EPFO/PPF/Real Estate) + Net Worth mix
│   ├── tab_upload.py             # 5paisa upload, CAMS CAS upload, Quick Monthly Delta, Accounts/Aliases/Logs
│   └── tab_gemini.py             # Ask Gemini chat UI (Settings & Configuration expander + large chat input)
├── scripts/
│   ├── seed_demo.py              # Deterministic sample data generator (`--reset` supported)
│   └── package_backup.sh         # Creates portable Google-Drive-ready folder + zip (excludes .venv & logs)
├── data/
│   └── portfolio.db              # Live SQLite database (single-file personal portfolio state)
└── tests/
    ├── test_ingestion.py         # Parsers, dedup, manual delta, baselines, advisory/region tags, server_control
    ├── test_analytics_and_market.py # XIRR, market resolution, split adjustments, benchmark replay, overlap
    └── test_mcp_and_e2e.py       # Seed CLI, all 10 FastMCP tools, read-only SQL guards, Gemini tool loop
```

---

## 2. SQLite Database Schema (`core/db.py`)

`init_db()` creates tables idempotently and runs safe `PRAGMA table_info` migrations for `region`, `advisory_source`, and `advisory_notes`.

1. **`accounts`**: `(id, owner, broker, account_name, account_code, extra_data, created_at)` — `UNIQUE(owner, broker, account_name)`.
2. **`transactions`**: `(id, account_id, trade_date, symbol, isin, exchange, asset_type, side, quantity, price, amount, fees, source, region, advisory_source, advisory_notes, dedup_hash UNIQUE, extra_data, created_at)`
   - `side IN ('BUY', 'SELL', 'DIVIDEND', 'BONUS', 'SPLIT')`.
   - `compute_tx_dedup_hash()` prevents duplicate inserts when overlapping tradebook files are re-imported.
3. **`holdings`**: `(id, account_id, symbol, isin, company_name, exchange, asset_type, quantity, avg_price, invested_value, as_of_date, source, region, advisory_source, advisory_notes, extra_data, updated_at)` — `UNIQUE(account_id, symbol, asset_type)`.
4. **`holding_baselines`**: Same columns as `holdings`. Stores reconciled depository snapshots (e.g., CDSL Beneficiary + Pledge as of `2026-11-25`).
5. **`mf_holdings`**: `(id, account_id, folio_number, scheme_name, amfi_code, isin, units, avg_nav, invested_value, as_of_date, source, region, advisory_source, advisory_notes, extra_data, updated_at)` — `UNIQUE(account_id, folio_number, scheme_name)`.
6. **`other_assets`**: `(id, owner, name, category, invested_value, current_value, as_of_date, notes, extra_data, updated_at)` — `UNIQUE(owner, name)`. Stores static long-term wealth (Retirement/PPF & Real Estate) editable inline on Overview and summed into Total Net Worth without diluting daily equity/MF analytics.
7. **`symbol_aliases`**: `(raw_symbol PRIMARY KEY, yahoo_symbol, isin, company_name, sector, updated_at)`. Caches broker-name-to-Yahoo-ticker mappings.
8. **`import_logs`**: Audit log of every CSV/XLSX/PDF/manual import and holding rebuild.

### Baseline & Rebuild Invariant (`rebuild_holdings_from_transactions`)
- If `holding_baselines` has rows for `account_id` (with `max_baseline_date`), `rebuild_holdings_from_transactions()` initializes positions from `holding_baselines` and **only replays transactions with `trade_date > max_baseline_date`**.
- `upsert_holdings(..., replace_account_holdings=True)` preserves existing `region`, `advisory_source`, and `advisory_notes` if omitted in incoming rows.
- Accounts with no `holding_baselines` (e.g., new accounts or tests) replay all transactions from `0`.

---

## 3. Market Data & Caching (`core/market_data.py`)

- **Strict separation of private vs. public data:** Live prices, historical OHLC, splits, USD/INR FX (`USDINR=X` via `fetch_usdinr_rate`), and AMFI NAVs are **never** written to SQLite. They live only in an in-memory thread-safe `_TTLCache` (`DEFAULT_CACHE_TTL_SECONDS = 900`, plus Streamlit `@st.cache_data(ttl=300)`).
- **Specialized Funds (`SIF`) & Foreign USD→INR Unification (`core/analytics.py`):** `asset_type = 'SIF'` (e.g., Altiva SIF) counts under `equity_holdings` and supports `extra_data.manual_ltp` when no live exchange feed exists. Foreign funds priced in USD (`extra_data.cost_currency = 'USD'` / `'live_currency' = 'USD'`, e.g., PPFAS GIFT City) convert into unified INR (`₹`) via `fetch_usdinr_rate()`.
- **Ticker Resolution (`resolve_yahoo_symbol`)**: Checks `SYMBOL_OVERRIDES` (`config.py`) → `symbol_aliases` (SQLite) → NSE `EQUITY_L.csv` (ISIN or company-name match) → Yahoo Finance search/probe (`.NS` then `.BO`), persisting verified mappings into `symbol_aliases`.

---

## 4. Quantitative Analytics (`core/analytics.py`)

1. **XIRR (`compute_xirr`)**: Uses multi-start Newton-Raphson with a 24-point grid bisection fallback (choosing the root closest to 10% CAGR if multiple roots exist).
   - *Known caveat when partial trade history is imported:* If an account holds pre-tradebook positions (like imported `MON100` or pre-`2025-04-25` holdings) without their original `BUY` cashflows in `transactions`, portfolio-level transaction XIRR reflects only recorded cashflows vs. total ending value.
2. **Benchmark Comparison (`compare_portfolio_vs_benchmark`)**:
   - Supports filtering by `owner`, `exclude_symbols`, `region` (`'India'` / `'Foreign'`), and `advisory_source`.
   - Uses `ThreadPoolExecutor(max_workers=10)` for parallel `yfinance` split lookups.
   - **Baseline-aware replay:** When `holding_baselines` exist for the filtered account, `_simulate_transactions_vs_benchmark()` unwinds transactions in `[first_day, baseline_date]` backward from `baseline_qty_map` to determine exact opening quantities on `first_day`, then replays `[first_day, end_date]` forward. This ensures imported/pre-history positions (like `MON100`) are accurately included across `Overall Combined`, `India`, `Foreign`, and `Advisory` curves.
   - Outputs both **Time-Weighted Return (TWR, Base 100)** and **Cashflow-Matched Benchmark (₹ Value)** alongside **Benchmark XIRR** and **Alpha**.

---

## 5. Hybrid Local + Gemini Cloud Architecture (`core/gemini_chat.py`)

- `build_local_portfolio_context()` computes a compact JSON digest (~13.8 KB, ~1.2 ms on M5) from the pre-loaded UI `snapshot` (totals, `by_advisory`, `by_region`, `top_gainers`, `top_losers`, `tx_summary`, compact `holdings` without `extra_data` bloat).
- Embedded into `system_instruction` so summary, P&L, advisor, region, and top/bottom questions resolve in **1 Gemini Cloud call** with zero round trips.
- When deep tools are requested (`tool_compare_against_benchmark`, `tool_check_mf_stock_overlap`, `tool_get_transactions_history`, `tool_run_readonly_sql`, etc.), multiple calls in a turn execute concurrently via `ThreadPoolExecutor`.

---

## 6. Current Live Portfolio State (`data/portfolio.db`, as of `2026-11-25`)

- **Account 1 (`Abhishek - 5paisa`):**
  - **Equities & ETFs (`53`):** Synced 100% to CDSL Depository Statement (`25/11/2026`, Beneficiary + Pledge combined) in both `holding_baselines` and `holdings`, with all 53 average buy prices populated.
  - **Mutual Funds (`5`):** Added from CDSL PDF with AMFI codes (`153738`, `149219`, `153722`, `154377`, `153272`). *Note: `avg_nav = 0.0` pending user-supplied cost basis or CAMS CAS upload.*
  - **Region (`region`):** `MON100` (`INF247L01AP3`) and `ICICI Prudential NASDAQ 100 Index Fund` (`INF109KC1U50`) are tagged `'Foreign'`; all other 56 positions are `'India'`.
  - **Advisory (`advisory_source`):** `RC Capital` (`16` holdings), `Fintrekk Capital` (`31` holdings, including `INTL. GEM. INSTITUTE` based on earliest Sep 12 2026 call), `Self` (`11` positions: `6` equities/ETFs + `5` MFs).
- **Transactions (`207` rows):** `2025-04-25` to `2026-11-08` from 5paisa tradebook exports.

---

## 7. Security & Safety Guardrails

- `run_readonly_query()` (`core/db.py`) enforces `PRAGMA query_only = ON` and rejects non-`SELECT`/`WITH`/`PRAGMA` queries or mutating keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `REPLACE`, `ATTACH`).
- `.env` is excluded by `.gitignore` and never hardcoded in source files.
- Demo exploration uses an isolated `data/demo_portfolio.db` so `data/portfolio.db` is untouched.
