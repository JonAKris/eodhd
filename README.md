# EODHD Stock Analysis Platform

A local, end-to-end equity research platform built on the [EODHD](https://eodhd.com)
**All-in-One API**, a Postgres data warehouse, and a home-lab GPU for local LLM
inference. It ingests prices, fundamentals, holders, insider filings, news, and
macro data into Postgres, then layers four subsystems on top:

1. **Ingest & warehouse** — pulls EODHD data into ~40 Postgres tables.
2. **Dashboard** (`app.py`) — a Dash web app for charting, fundamentals, and portfolio/trade CRUD.
3. **The agent** (`agent/`) — a point-in-time signal framework: pluggable strategies
   behind one `Signal`/`Strategy`/`Context` contract, ranked across the universe.
4. **The explorer** (`explorer/`) — an autonomous, LLM-driven job that runs SQL
   strategies, cross-references multi-signal names, and emails a grounded morning report.

Plus a factor **backtest harness** (`backtest.py`) and an SSG **screener**
(`ssg_screener.py`).

> Design principle throughout: **the data layer computes; the LLM only narrates.**
> Every number the models emit is pre-computed and verified — signals are graded
> on whether they're *real* (information coefficient) before they're profitable,
> and point-in-time correctness (no look-ahead) is enforced in code, not assumed.

---

## Repository layout

| Path | Purpose |
|---|---|
| `sql/schema.sql` | Postgres DDL — creates the `eodhd` database and ~40 tables (prices, fundamentals, holders, insider transactions, corporate actions, news, calendars, macro, options, portfolio/trades) |
| `sql/01_metrics_views.sql` | Materialized views: `inst_flow` / `fund_flow` holder rollups, `price_perf` returns |
| `sql/02_newsletter_findings.sql` | `build_newsletter()` / `newsletter_payload()` — the SQL→LLM contract for the newsletter |
| `sql/flow_vintages.sql` | Vintage-banking layer: dated snapshots of the holder rollups (`bank_flow_vintages()`, `refresh_and_bank()`) |
| `sql/migration_insider.sql` | Adds `report_date` / `owner_title` to `insider_transactions` |
| `sql/backfill_fund_change.sql` | One-time repair of `fund_holders.change_shares` from stored JSONB (see *Historical migrations* below) |
| `config.py` | Settings from `.env` — writer (`dsn`) and read-only (`ro_dsn`) identities |
| `db.py` | psycopg3 connection pool + helpers (`fetch_all`, `fetch_one`, `execute`, `execute_many`) |
| `ingest.py` | `Ingestor` wrapping the official `eodhd.APIClient`, with a CLI |
| `ingest_delisted.py` | Backfills delisted symbols for survivorship-correct history |
| `backtest.py` | Point-in-time factor backtester (momentum, value, quality, piotroski) with IC-first scoring |
| `ssg_screener.py` | BetterInvesting/NAIC Stock Selection Guide screener with focus-forecasting |
| `portfolio.py` | Pure-database CRUD for portfolios & trades |
| `app.py` | Dash dashboard |
| `agent/` | The point-in-time signal agent (see below) |
| `explorer/` | The autonomous LLM explorer (see below) |
| `systemd/` | Service + timer units for the explorer |

---

## The agent (`agent/`)

Five strategies conform to a single contract and are ranked across the universe
by one command.

```
agent/
  core/
    contract.py     Signal, Strategy (the interface)
    context.py      Context, Floors (DB access + validity thresholds)
    registry.py     the strategy roster
    universe.py     the ticker set to rank over
  strategies/
    momentum.py             12-1 price momentum (point-in-time, backtestable)
    value.py                trailing earnings yield (point-in-time, backtestable)
    insider.py              net open-market insider $ (event signal, filing-date gated)
    institutional_flow.py   net institutional share-count change (vintage-backed)
    ssg.py                  wraps ssg_screener as a rich-study strategy
  modes/
    select.py       rank the universe by a strategy as of a date
  cli.py            entry point
  selftest.py       offline test suite (no DB required)
```

Every strategy returns a `Signal(value, as_of, flags, detail)`. `value is None`
means *not rankable* (below a validity floor, or not knowable as of that date) —
never zero. Signals are either **retrospectively backtestable** (momentum, value,
insider — sourced from data with honest as-of dates) or **prospective-only**
(institutional flow, SSG — snapshot-sourced), with the distinction enforced by
the as-of gate rather than left to the caller.

```bash
# rank the universe by a strategy
python -m agent.cli select --strategy momentum --limit 500 --top 15
python -m agent.cli select --strategy ssg --sector Technology --out ssg_picks.csv
python -m agent.cli select --strategy institutional_flow --ascending --top 20   # distribution

# offline test suite (fake DB, no connection needed)
python -m agent.selftest
```

### The vintage layer

Holder rollups (`inst_flow_ticker`, `fund_flow_ticker`) are rebuilt from a
*current* snapshot each refresh, so on their own they can only answer "what is
true now." `sql/flow_vintages.sql` appends a dated copy on every refresh
(`bank_flow_vintages()`), so the snapshot signals accumulate a point-in-time
history and become backtestable **going forward from the first bank**. Wire it
into your refresh job via `refresh_and_bank()` (run as the writer role).

---

## The explorer (`explorer/`)

An autonomous overnight job: it runs a library of SQL strategies at randomized
parameters, cross-references tickers that light up on multiple signals, has a
local LLM interpret the results, and emails a grounded morning report. The LLM
never sees the database and does no arithmetic — it narrates pre-computed facts.

```
explorer/
  runner.py         orchestrates strategies → cross-reference → report
  sql_strategies.py library of SQL strategies (value/quality, momentum, holder
                    conviction, congressional trades, earnings, dividends, …)
  llm.py            Ollama interface (schema-constrained JSON, JSON repair)
  morning_report.py assembles + emails the HTML report
```

```bash
python -m explorer.runner          # one exploration cycle → findings/
```

Runs read-only. Schedule it with the `systemd/` units (which pin the process to
the read-only DB role); adjust the timer to land before your morning slot.

---

## Prerequisites

- Python 3.10+
- Postgres 16 (with `pgcrypto`, `btree_gin`, `pg_trgm` — from `postgresql-contrib`)
- An EODHD API key (`demo` works for `AAPL.US` and a few others to try things)
- For the explorer: [Ollama](https://ollama.com) with a local model (this repo
  defaults to `qwen3.6:35b`; a 24GB GPU is comfortable)

## Setup

```bash
cd eodhd
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — see "Configuration & roles" below

# create the database and schema (schema.sql creates the eodhd DB itself,
# so connect to `postgres` first)
psql -U postgres -h localhost -d postgres -f sql/schema.sql

# then the analytics layer
psql "$DSN" -f sql/01_metrics_views.sql
psql "$DSN" -f sql/02_newsletter_findings.sql
psql "$DSN" -f sql/migration_insider.sql
psql "$DSN" -f sql/flow_vintages.sql
```

## Configuration & roles

Config is environment variables only (`config.py` reads `.env` — no dependency).
The platform uses **two database identities**, and keeping them separate is a
deliberate safety boundary — one that `db.py` now enforces in code by opening a
separate pool per role, rather than leaving it to convention:

- **Writer** (`PG_USER` / `PG_PASSWORD`) — ingest, view refresh, vintage banking,
  and portfolio/trade CRUD. The default `db.py` helpers (`fetch_all`, `execute`,
  …) use this pool.
- **Read-only** (`PG_RO_USER` / `PG_RO_PASSWORD`) — the agent, the explorer, and
  the dashboard's market-data reads. These go through `db.fetch_all_ro` /
  `fetch_one_ro` (and, for the agent, `Context.from_dsn()`), which connect via
  `config.ro_dsn`. Grant this role `SELECT` only. It falls back to the writer
  identity when `PG_RO_*` is unset, so a single-role setup still works.

The dashboard therefore uses **both** identities: it browses market data through
the read-only role, and performs portfolio/trade writes through the writer role
(via `portfolio.py`). The read-only role never needs write grants on any table —
including `portfolios` and `trades`, which the writer owns.

## Ingesting data

```bash
python ingest.py exchanges
python ingest.py symbols US
python ingest.py all AAPL.US            # everything for one ticker
python ingest.py eod-refresh            # update recent prices
python ingest.py insider GME.US         # Form 4 insider transactions
```

Then refresh (and bank) the analytics views on a schedule:

```bash
psql "$WRITER_DSN" -c "SELECT refresh_and_bank();"   # run as the writer role
```

## Backtesting

```bash
python backtest.py --validate                 # schema sanity check
python backtest.py --signal momentum          # IC + decile spread over history
```

---

## Historical migrations

Two SQL files are **one-time repairs**, not part of setup — they fixed ingest
bugs by recovering data already stored as JSONB, with no API re-pull. They're
idempotent but need only run once:

- `sql/backfill_fund_change.sql` — repaired `fund_holders.change_shares`, which
  the ingest originally never mapped.
- (insider share/value repair — applied via a prior backfill.)

The ingest now maps these fields correctly at write time, so new pulls don't need them.

## License

See `LICENSE`.