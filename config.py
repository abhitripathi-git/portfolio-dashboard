"""Configuration and extensible mappings for the portfolio dashboard."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "portfolio.db"

# Default family owners (modifiable—new owners can be added via UI or DB anytime)
DEFAULT_OWNERS = ["Abhishek", "Tanu"]

# Indian market benchmark indices on Yahoo Finance (with fallback candidates)
BENCHMARKS = {
    "NIFTY_50": {
        "label": "Nifty 50",
        "tickers": ["^NSEI"],
    },
    "NIFTY_MIDCAP_150": {
        "label": "Nifty Midcap 150",
        "tickers": ["NIFTYMIDCAP150.NS", "MIDCAPETF.NS", "^CRSLDX"],
    },
    "NIFTY_MIDCAP_50": {
        "label": "Nifty Midcap 50",
        "tickers": ["^NSEMDCP50"],
    },
    "NIFTY_SMALLCAP_250": {
        "label": "Nifty Smallcap 250",
        "tickers": ["NIFTYSMLCAP250.NS", "SMALLCAP.NS"],
    },
    "NIFTY_NEXT_50": {
        "label": "Nifty Next 50",
        "tickers": ["^NSMIDCP", "JUNIORBEES.NS"],
    },
    "SENSEX": {
        "label": "BSE Sensex",
        "tickers": ["^BSESN"],
    },
}

# Extensible column alias maps for 5paisa & generic broker Excel/CSV exports.
# Add new aliases here anytime if your broker changes column headers.
TRADEBOOK_COLUMN_ALIASES = {
    "trade_date": [
        "trade date", "tradedate", "date", "order date", "execution date",
        "txndate", "txn date", "buy/sell date", "settlement date",
    ],
    "symbol": [
        "scrip", "scrip name", "scripname", "symbol", "trading symbol",
        "tradingsymbol", "stock", "stock name", "security", "company",
        "instrument", "ticker",
    ],
    "isin": ["isin", "isin code", "isin no"],
    "exchange": ["exchange", "exch", "segment", "mkt"],
    "side": [
        "buy/sell", "buysell", "side", "type", "txn type", "transaction type",
        "trade type", "b/s", "action",
    ],
    "quantity": ["qty", "quantity", "shares", "units", "traded qty", "net qty"],
    "price": [
        "price", "rate", "trade price", "avg price", "average price",
        "net rate", "mkt rate", "executed price",
    ],
    "amount": [
        "amount", "value", "net amount", "gross amount", "trade value",
        "total", "turnover",
    ],
    "fees": [
        "brokerage", "charges", "fees", "total charges", "taxes", "stt",
    ],
}

HOLDINGS_COLUMN_ALIASES = {
    "symbol": [
        "scrip", "scrip name", "scripname", "symbol", "trading symbol",
        "tradingsymbol", "stock", "stock name", "company", "company name",
    ],
    "isin": ["isin", "isin code", "isin no"],
    "company_name": ["company name", "company", "security name", "scrip name", "name"],
    "exchange": ["exchange", "exch"],
    "quantity": [
        "qty", "quantity", "net qty", "total qty", "holding qty",
        "current qty", "free qty", "units",
    ],
    "avg_price": [
        "avg price", "avg. price", "average price", "buy avg", "avg cost",
        "cost price", "avg buy price", "purchase price",
    ],
    "invested_value": [
        "invested", "invested value", "cost value", "buy value", "total cost",
    ],
}

# Common 5paisa/NSE scrip name cleanup overrides (extend as needed)
SYMBOL_OVERRIDES = {
    "MON100": "MON100.NS",
    "RELETFNM150": "MID150BEES.NS",
    "TATAMOTORS": "TATAMOTORS.NS",
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "SBIN": "SBIN.NS",
}
