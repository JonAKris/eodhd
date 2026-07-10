#!/usr/bin/env python3
"""
ingest.py — symbol-list ingestion for the `eodhd` database.

`symbols` subcommand pulls EODHD's exchange-symbol-list and upserts into the
`symbols` table. With --delisted it pulls the delisted roster instead of the
active one and marks those rows is_active=false (Phase 1 of the backfill plan).

  python ingest.py symbols --exchange US --dry-run        # active, preview only
  python ingest.py symbols --exchange US --delisted --dry-run
  python ingest.py symbols --exchange US --delisted        # writes

-------------------------------------------------------------------------------
VERIFY THESE 3 THINGS against your existing rows before the first real write
(use --dry-run, which prints sample rows and writes nothing):

  1. exchange_code mapping. This script sets exchange_code = the --exchange arg
     (e.g. "US"), because your tickers are "<CODE>.US". If your symbols.exchange_code
     actually stores the sub-exchange ("NASDAQ"/"NYSE" from entry["Exchange"]),
     flip MAP_EXCHANGE_CODE below. Wrong value => FK violation against exchanges.
  2. ticker construction. Built as f"{Code}.{exchange}". Confirm that matches your
     existing ticker format exactly (incl. EODHD's "_old" suffix on reused codes,
     which arrives inside Code and is preserved as-is).
  3. config source. Reads settings.toml first, then env. Confirm the keys below
     match your settings.toml structure.
-------------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, date
from pathlib import Path

import click
import requests

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

import psycopg2
from psycopg2.extras import execute_values

API_BASE = "https://eodhd.com/api/exchange-symbol-list"

# If your symbols.exchange_code stores the sub-exchange instead of "US",
# set this to "entry" (uses entry["Exchange"]); default "arg" uses --exchange.
MAP_EXCHANGE_CODE = "arg"   # "arg" | "entry"

# Candidate keys EODHD may use for a delisting date on the delisted roster.
# If none are present, delisted_on is left NULL and can be derived later from
# max(eod_prices.date) per ticker after the Phase 2 price backfill.
DELIST_DATE_KEYS = ("DelistedDate", "Delisted", "DelistingDate", "delisted_on")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """settings.toml first (your convention), then environment fallback."""
    cfg: dict = {}
    toml_path = Path("settings.toml")
    if toml_path.exists() and tomllib is not None:
        with open(toml_path, "rb") as fh:
            cfg = tomllib.load(fh)

    api_token = (cfg.get("eodhd", {}).get("api_token")
                 or os.getenv("EODHD_API_KEY"))

    db = cfg.get("database", {})
    dsn = (db.get("dsn")
           or os.getenv("DATABASE_URL")
           or "dbname={d} user={u} password={p} host={h} port={pt}".format(
               d=db.get("dbname") or os.getenv("PGDATABASE", "eodhd"),
               u=db.get("user") or os.getenv("PGUSER", "jon"),
               p=db.get("password") or os.getenv("PGPASSWORD", ""),
               h=db.get("host") or os.getenv("PGHOST", "localhost"),
               pt=db.get("port") or os.getenv("PGPORT", "5432"),
           ))

    if not api_token:
        sys.exit("No EODHD API token. Set [eodhd].api_token in settings.toml "
                 "or EODHD_API_TOKEN in the environment.")
    return {"api_token": api_token, "dsn": dsn}


# ---------------------------------------------------------------------------
# Fetch + map
# ---------------------------------------------------------------------------

def fetch_symbol_list(exchange: str, delisted: bool, token: str) -> list[dict]:
    params = {"api_token": token, "fmt": "json"}
    if delisted:
        params["delisted"] = "1"
    url = f"{API_BASE}/{exchange}"
    resp = requests.get(url, params=params, timeout=120)
    if resp.status_code != 200:
        sys.exit(f"EODHD returned HTTP {resp.status_code} for {url} "
                 f"(body starts: {resp.text[:200]!r})")
    data = resp.json()
    if not isinstance(data, list):
        sys.exit(f"Unexpected payload (expected a list): {str(data)[:200]}")
    return data


def _parse_delist_date(entry: dict):
    for key in DELIST_DATE_KEYS:
        val = entry.get(key)
        if val in (None, "", "0000-00-00"):
            continue
        if isinstance(val, (date, datetime)):
            return val if isinstance(val, date) else val.date()
        try:
            return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
    return None


def map_entry(entry: dict, exchange: str, delisted: bool) -> dict | None:
    code = entry.get("Code")
    if not code:
        return None
    exchange_code = exchange if MAP_EXCHANGE_CODE == "arg" else entry.get("Exchange")
    return {
        "ticker": f"{code}.{exchange}",
        "code": code,
        "exchange_code": exchange_code,
        "name": entry.get("Name"),
        "country": entry.get("Country"),
        "currency": entry.get("Currency"),
        "type": entry.get("Type"),
        "isin": entry.get("Isin") or entry.get("ISIN"),
        "is_active": not delisted,
        "delisted_on": _parse_delist_date(entry) if delisted else None,
    }


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

COLUMNS = ["ticker", "code", "exchange_code", "name", "country",
           "currency", "type", "isin", "is_active", "delisted_on"]

UPSERT_SQL = f"""
INSERT INTO symbols ({", ".join(COLUMNS)})
VALUES %s
ON CONFLICT (ticker) DO UPDATE SET
    code          = EXCLUDED.code,
    exchange_code = EXCLUDED.exchange_code,
    name          = EXCLUDED.name,
    country       = EXCLUDED.country,
    currency      = EXCLUDED.currency,
    type          = EXCLUDED.type,
    isin          = EXCLUDED.isin,
    is_active     = EXCLUDED.is_active,
    -- keep an existing delist date if the new pull doesn't supply one
    delisted_on   = COALESCE(EXCLUDED.delisted_on, symbols.delisted_on)
"""


def upsert_symbols(dsn: str, rows: list[dict]) -> int:
    values = [tuple(r[c] for c in COLUMNS) for r in rows]
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, values, page_size=1000)
        conn.commit()
    return len(values)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """EODHD ingestion utilities."""


@cli.command()
@click.option("--exchange", default="US", show_default=True,
              help="Exchange code passed to exchange-symbol-list (e.g. US).")
@click.option("--delisted", is_flag=True, default=False,
              help="Pull the delisted roster and mark rows is_active=false.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Fetch and map, print a summary, write nothing.")
@click.option("--limit", type=int, default=None,
              help="Cap the number of rows (debugging).")
def symbols(exchange, delisted, dry_run, limit):
    """Ingest the active (or --delisted) symbol list into `symbols`."""
    cfg = load_config()
    raw = fetch_symbol_list(exchange, delisted, cfg["api_token"])
    rows = [m for e in raw if (m := map_entry(e, exchange, delisted)) is not None]
    if limit:
        rows = rows[:limit]

    kind = "DELISTED" if delisted else "active"
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["type"] or "?"] = by_type.get(r["type"] or "?", 0) + 1
    with_date = sum(1 for r in rows if r["delisted_on"] is not None)

    click.echo(f"\n{kind} symbols for exchange {exchange!r}: {len(rows)} rows")
    click.echo("  by type: " + ", ".join(
        f"{t}={n}" for t, n in sorted(by_type.items(), key=lambda x: -x[1])[:12]))
    if delisted:
        click.echo(f"  with a parseable delisted_on: {with_date} "
                   f"({with_date * 100 // max(len(rows), 1)}%)")
    click.echo("  sample rows:")
    for r in rows[:3]:
        click.echo(f"    {r['ticker']:<16} exch={r['exchange_code']!s:<8} "
                   f"type={r['type']!s:<14} is_active={r['is_active']} "
                   f"delisted_on={r['delisted_on']}")

    if dry_run:
        click.echo("\n[dry-run] no rows written. "
                   "Verify ticker / exchange_code / delisted_on above, then re-run "
                   "without --dry-run.")
        return

    n = upsert_symbols(cfg["dsn"], rows)
    click.echo(f"\nUpserted {n} rows into symbols.")


if __name__ == "__main__":
    cli()