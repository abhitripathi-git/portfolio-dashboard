# AI Assistant Guide (`CLAUDE.md`)

You are working inside **Personal Portfolio Dashboard (`portfolio-dashboard`)**, a local-first Indian equities, ETFs, and mutual funds analytics app + FastMCP server for multi-owner family portfolios.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full module map, schema, and financial math reference.

## Quick Commands

```bash
# 1. Run full test suite (always run after edits)
.venv/bin/pytest -q

# 2. Launch Streamlit dashboard
./start_dashboard.command
# or: .venv/bin/streamlit run app.py

# 3. Rebuild environment from scratch on a new Mac
uv sync --extra dev

# 4. Create a clean portable backup zip (excludes .venv & logs, keeps data/portfolio.db)
./scripts/package_backup.sh
```

## Critical Invariants (Do Not Break)

1. **Depository Baseline Integrity (`holding_baselines`):**
   - Accounts with imported depository snapshots store a reconciled baseline (`as_of_date`) in `holding_baselines` when transaction CSVs only cover recent periods.
   - Never wipe `holding_baselines` unless re-importing a newer depository statement. `rebuild_holdings_from_transactions()` relies on it to preserve pre-baseline holdings when new monthly trades are added.
2. **Metadata Columns (`region`, `advisory_source`, `advisory_notes`):**
   - Always use `update_asset_region()` or `update_advisory_source()` in `core/db.py` when updating tags so both `holdings` and `holding_baselines` stay in sync.
   - Valid `region` values: `'India'` (default) or `'Foreign'`.
3. **Public Market Data Is Never Stored in SQLite:**
   - All live prices, historical series, splits, and AMFI NAVs stay in memory (`core/market_data.py`).
4. **Live DB Safety:**
   - Never run `scripts/seed_demo.py --reset` against `data/portfolio.db` — that overwrites real personal data. Only test against `tmp_path` or `data/demo_portfolio.db`.
   - Before making bulk changes to `data/portfolio.db`, copy a backup first.
5. **Secrets:**
   - `GEMINI_API_KEY` lives in `.env` (gitignored). Never print or commit secret values.
