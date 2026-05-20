# EODHD → Postgres + Dash Stock Charting App

A Python application that ingests data from the [EODHD](https://eodhd.com) **All-in-One API package** into a Postgres database, plus a Dash web app for charting prices, browsing company fundamentals, and managing portfolios and trades.

## What's included

| File | Purpose |
|---|---|
| `sql/schema.sql` | Postgres DDL: creates the `eodhd` database and ~35 tables covering prices, fundamentals, corporate actions, news, calendars, macro data, options, plus portfolio/trade CRUD tables |
| `config.py` | Loads settings from `.env` (no extra dependency) |
| `db.py` | psycopg3 connection pool + small helpers (`fetch_all`, `execute`, etc.) |
| `ingest.py` | `Ingestor` class wrapping the official `eodhd.APIClient`, plus a CLI |
| `portfolio.py` | Pure-database CRUD layer for portfolios & trades |
| `app.py` | Dash app: charting, fundamentals tabs, portfolio CRUD |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for your local `.env` |

## Prerequisites

- Python 3.10+
- Postgres 14+ (with the `pgcrypto`, `btree_gin`, `pg_trgm` extensions available — these ship with the standard `postgresql-contrib` package on most distributions)
- An EODHD API key (the string `demo` works for `AAPL.US` and a handful of other symbols if you just want to try things out)

## Setup

```bash
# 1. Clone / copy the project, then:
cd eodhd_app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env: set EODHD_API_KEY and your Postgres credentials

# 3. Create the database and schema
#    The script creates the `eodhd` database itself, so connect to `postgres` first.
psql -U postgres -h localhost -d postgres -f sql/schema.sql
```

The schema script is idempotent for table creation (uses `CREATE TABLE IF NOT EXISTS`) but the initial `CREATE DATABASE eodhd` will error if the DB already exists — that's harmless, the rest of the script will still run after you `\c eodhd`.

## Ingesting data

The `ingest.py` CLI exposes every endpoint as a subcommand. Typical first-time workflow:

```bash
# Reference data (run once, refresh occasionally)
python ingest.py exchanges
python ingest.py symbols US        # populate all US tickers

# All data for one ticker (EOD prices, fundamentals, dividends, splits, live quote, news, sentiment)
python ingest.py all AAPL.US

# Or pick & choose
python ingest.py eod AAPL.US --from 2020-01-01
python ingest.py fundamentals MSFT.US
python ingest.py dividends KO.US
python ingest.py intraday TSLA.US --interval 5m
python ingest.py news NVDA.US --limit 50

# Calendars (no ticker needed)
python ingest.py calendar-earnings --from 2026-05-19 --to 2026-05-26
python ingest.py calendar-ipos
python ingest.py calendar-splits

# Macro
python ingest.py economic-events --country US
python ingest.py macro --country US --indicator real_interest_rate

# Options chain
python ingest.py options AAPL.US

# Refresh EOD prices for every active symbol since a date
python ingest.py eod-refresh --since 2026-05-01
```

Tickers are stored in EODHD's `SYMBOL.EXCHANGE` form (e.g. `AAPL.US`, `BMW.XETRA`). The ingestor automatically creates a skeleton row in `symbols` and `exchanges` for any ticker you load, so FK constraints are always satisfied.

Every ingest run is logged to the `ingest_log` table with row counts and any error.

## Running the Dash app

```bash
python app.py
```

Open <http://localhost:8050>. Pages:

- **`/`** — landing page with an "Ingest a ticker" form that runs `Ingestor.ingest_all_for_ticker` from the browser. Handy for trying things out without dropping back to the CLI.
- **`/chart`** — type a ticker (autocomplete from the `symbols` table), pick a date range, chart type (Candlestick / OHLC / Line / Area), and overlays (SMA 20/50/200, Bollinger Bands). Below the chart: a fundamentals header (price, market cap, P/E, dividend yield, 52-week range) plus 12 tabs — Overview, Income Statement, Balance Sheet, Cash Flow, Valuation, Earnings, Dividends & Splits, Holders, Insider Trades, Analyst Ratings, ESG, News.
- **`/portfolios`** — list view with full CRUD: create / rename / delete portfolios. The summary view (`portfolio_summary`) shows trade count and net cash flow per portfolio.
- **`/portfolios/<id>`** — single portfolio view: stat cards (initial cash, current value, P&L), a trades table with full CRUD (add buy/sell, edit, delete), a positions table joining current holdings against the latest EOD price for unrealised P&L, and an equity-curve chart marking-to-market every day since the portfolio's first trade.

For production-style deployment you can run via gunicorn:

```bash
gunicorn -b 0.0.0.0:8050 app:server
```

## Data model highlights

The schema mirrors the EODHD All-in-One package endpoints:

- **Reference** — `exchanges`, `exchange_details`, `symbols`, `symbol_change_history`
- **Prices** — `eod_prices`, `intraday_prices`, `realtime_quotes`, `tick_data`, `technical_indicators`
- **Corporate actions** — `dividends`, `splits`, `shares_outstanding`, `historical_market_cap`
- **Fundamentals** — `fundamentals` (header with frequently-queried scalar columns *and* JSONB for nested sections like analyst ratings, holders, ESG scores, ETF data, components), plus normalised tables for `income_statements`, `balance_sheets`, `cash_flow_statements`, `earnings_history`, `earnings_trend`, `analyst_ratings_history`, `institutional_holders`, `fund_holders`, `insider_transactions`
- **News & sentiment** — `news`, `sentiment_daily`
- **Calendars** — `earnings_calendar`, `ipo_calendar`, `splits_calendar`
- **Macro** — `economic_events`, `macro_indicators`
- **Options** — `options_chains` (one row per contract, Greeks included)
- **Bonds** — `bond_fundamentals`
- **Portfolio CRUD** — `users`, `portfolios`, `trades`, plus views `portfolio_positions` and `portfolio_summary`
- **Bookkeeping** — `ingest_log`

The decision rule for "scalar column vs JSONB" was: anything you'd routinely filter or sort on (sector, market cap, P/E, dividend yield, etc.) gets its own column; anything you'd only ever display whole (full analyst rating history, all institutional holders, ESG breakdowns) is kept as JSONB so we don't paper over EODHD's schema evolution.

A demo user (UUID `00000000-0000-0000-0000-000000000001`) is seeded by the schema, and the Dash app uses it by default — wire in real auth later if you need multi-tenancy.

## Notes & limitations

- The official `eodhd` PyPI library is used for every API call. If EODHD adds new endpoints, extend `ingest.py` and add a matching table to `schema.sql`.
- Bulk EOD download (`get_bulk_eod_splits_dividends_data`) is not wired into the CLI; if you have an "All World Extended" subscription, you can add a subcommand that iterates exchanges.
- The portfolio equity-curve walks every distinct date in `eod_prices` for the held tickers — fine for hundreds of trades, but if you build huge portfolios you may want to materialise it.
- The "Ingest" button on the landing page runs synchronously inside the Dash callback, so a fresh ticker can take 10–30 seconds. For production, move ingest to a queue (RQ, Celery, or a cron job hitting `ingest.py`).
