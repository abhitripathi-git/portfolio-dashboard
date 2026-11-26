# Personal Portfolio Dashboard & MCP Server

A portable, local-first Indian equities, ETFs, and mutual funds portfolio tracker built for multi-owner **Individual** and **Combined** family views.

- **100% Local SQLite Storage (`data/portfolio.db`)** — holdings, CDSL depository baselines, transactions, mutual fund folios, symbol aliases, and advisory/region tags stay on your Mac.
- **On-Demand Live Market Data** — stock/ETF prices and index benchmarks come from Yahoo Finance (`yfinance`), and mutual fund NAVs come from AMFI (`mftool`), cached only in memory (never written into SQLite).
- **Three Ways to Use & Audit:**
  1. **Visual Streamlit Dashboard (`app.py`)** — Overview KPIs, Advisory & Region breakdowns, Benchmark & Segment Simulator (`Overall Combined` blue line + `India` / `Foreign` / `Advisory` overlay), Holdings & Movers, MF & Stock Overlap, Upload & Refresh, and hybrid M5+Gemini Chat.
  2. **Local FastMCP Server (`mcp_server.py`)** — ask questions or add monthly trade deltas in plain English inside **Claude Desktop**, **Claude Code**, **Cursor**, or **Antigravity**.
  3. **AI-Ready Documentation** — see [ARCHITECTURE.md](ARCHITECTURE.md) for a full module-by-module audit guide and [CLAUDE.md](CLAUDE.md) for AI assistant instructions.

---

## 1. Fresh Mac / Post-Format Rebuild Instructions

After copying this folder back from Google Drive (recommended location: `~/Projects/portfolio-dashboard`, **not** inside a live cloud-sync folder while running SQLite):

### Prerequisites
- **Python 3.11+**
- **`uv`** package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`)

### Rebuild & Launch in One Step
Double-click **`start_dashboard.command`** (or run it from Terminal):

```bash
cd /path/to/portfolio-dashboard
chmod +x start_dashboard.command scripts/package_backup.sh
./start_dashboard.command
```

`start_dashboard.command` automatically detects its own directory, rebuilds `.venv` from `pyproject.toml` + `uv.lock` if `.venv` is missing, and launches Streamlit at `http://localhost:8501`.

Or manually with `uv`:

```bash
cd /path/to/portfolio-dashboard
uv sync --extra dev
.venv/bin/pytest -q
.venv/bin/streamlit run app.py
```

### API Keys (`.env`)
Copy `.env.example` to `.env` (or configure inside the **Ask Gemini → Settings & Configuration** expander in the UI):
- `GEMINI_API_KEY`: used only by the **Ask Gemini** tab in the dashboard.
- `PORTFOLIO_DB_PATH` *(optional)*: overrides the default `data/portfolio.db` location.

---

## 2. Configure the Local MCP Server (Claude Desktop / Cursor / Antigravity)

Replace `/ABS/PATH/TO/portfolio-dashboard` with the actual folder path on your Mac:

```json
{
  "mcpServers": {
    "personal-portfolio": {
      "command": "/ABS/PATH/TO/portfolio-dashboard/.venv/bin/python",
      "args": [
        "/ABS/PATH/TO/portfolio-dashboard/mcp_server.py"
      ]
    }
  }
}
```

Where to paste on Mac:
- **Claude Desktop**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Cursor**: `~/.cursor/mcp.json` (or `.cursor/mcp.json` in your workspace)
- **Antigravity**: MCP Servers configuration (`mcp_config.json`)

Exposed MCP tools (`mcp_server.py`):
- `list_portfolio_accounts`
- `get_portfolio_summary`
- `get_holdings_detail`
- `get_mf_holdings_detail`
- `get_transactions_history`
- `compare_against_benchmark` (supports `benchmark`, `start_date`, `end_date`, `exclude_symbols`, `region`, `advisory_source`)
- `check_mf_stock_overlap`
- `add_trade_delta`
- `add_symbol_mapping`
- `run_readonly_sql`

Available benchmark keys: `NIFTY_50`, `NIFTY_MIDCAP_150`, `NIFTY_MIDCAP_50`, `NIFTY_SMALLCAP_250`, `NIFTY_NEXT_50`, `SENSEX`.

---

## 3. Key Features & Workflows

### CDSL Source-of-Truth Baselines (`holding_baselines`)
- Depository statements (like CDSL Beneficiary + Pledge) can be stored in `holding_baselines` as of `as_of_date`.
- `rebuild_holdings_from_transactions()` starts from those baselines and only applies transactions with `trade_date > max(baseline.as_of_date)`, ensuring imported/pre-tradebook holdings (e.g., `MON100`) are never wiped out when new monthly trades are added.
- `compare_portfolio_vs_benchmark()` unwinds transactions backward from `holding_baselines` so historical benchmark replays include all baseline positions accurately.

### Region (`India` / `Foreign`) & Advisory (`Self` / `RC Capital` / `Fintrekk Capital`) Tracking
- Every holding, baseline, MF holding, and transaction carries `region`, `advisory_source`, and `advisory_notes`.
- **Benchmark & Simulator** always plots the blue **Overall Combined** line + Benchmark, and offers a **Compare Segment** dropdown (`Region: India`, `Region: Foreign`, `Advisory: Self`, `Advisory: RC Capital`, `Advisory: Fintrekk Capital`, etc.) to overlay one segment at a time.

### One-Click Server Restart
- The sidebar includes **Refresh Prices** (clears in-memory cache) and **Restart App** (`core/server_control.py`), which restarts the Streamlit server and auto-reloads the browser tab without needing Terminal.

---

## 4. Backing Up to Google Drive (Before Formatting / Uninstalling Tools)

Run the packager script to generate a clean, `.venv`-free folder and `.zip` archive (contains all source code, docs, `pyproject.toml`, `uv.lock`, `.env`, and `data/portfolio.db`):

```bash
./scripts/package_backup.sh
```

> **Important:** Do **not** run SQLite (`data/portfolio.db`) directly inside a live Google Drive / iCloud sync folder while Streamlit or MCP is running, as concurrent sync locks can corrupt SQLite. Keep the working copy on local disk (e.g., `~/Projects/portfolio-dashboard`) and copy the generated backup `.zip` (or `data/portfolio.db`) to Google Drive.
