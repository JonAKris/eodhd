"""
db.py
-----
Shared psycopg connection pools and a handful of row helpers.

Two pools, keyed by role, so the read-only safety boundary described in the
README is enforced in code rather than by convention:

  * the **writer** pool (``settings.dsn``) backs ingest, view refresh, vintage
    banking, and portfolio/trade CRUD -- anything that mutates the database;
  * the **read-only** pool (``settings.ro_dsn``) backs the dashboard's
    market-data reads, the agent, the explorer, and the screener.

The default helpers (``fetch_all``, ``fetch_one``, ``execute``,
``execute_many``, ``connection``, ``cursor``) use the writer pool, so existing
write paths keep working unchanged. Read-only consumers call the ``*_ro``
variants (or pass ``readonly=True``), which connect through ``ro_dsn``. When
``PG_RO_*`` is unset, ``ro_dsn`` falls back to the writer identity, so a
single-role setup still works.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

# One pool per role. Keyed by the ``readonly`` flag: False -> writer, True -> RO.
_pools: dict[bool, ConnectionPool] = {}


def get_pool(readonly: bool = False) -> ConnectionPool:
    """Return the shared pool for the requested role, opening it on first use.

    The read-only pool runs with ``autocommit=True`` (pure SELECT traffic, no
    idle-in-transaction); the writer pool keeps ``autocommit=False`` so the
    ``cursor()`` helper controls transaction boundaries explicitly.
    """
    pool = _pools.get(readonly)
    if pool is None:
        pool = ConnectionPool(
            conninfo=settings.ro_dsn if readonly else settings.dsn,
            min_size=1,
            max_size=settings.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": readonly},
            open=True,
        )
        _pools[readonly] = pool
    return pool


@contextmanager
def connection(readonly: bool = False) -> Iterator[psycopg.Connection]:
    with get_pool(readonly).connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Writer cursor with an explicit commit on clean exit. Writes only."""
    with connection(readonly=False) as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


# -- reads ------------------------------------------------------------------
def fetch_all(
    sql: str, params: tuple | dict | None = None, *, readonly: bool = False
) -> list[dict]:
    with connection(readonly) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def fetch_one(
    sql: str, params: tuple | dict | None = None, *, readonly: bool = False
) -> dict | None:
    with connection(readonly) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def fetch_all_ro(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Read through the read-only role. For the dashboard, agent, explorer,
    and screener -- anything that must never write."""
    return fetch_all(sql, params, readonly=True)


def fetch_one_ro(sql: str, params: tuple | dict | None = None) -> dict | None:
    return fetch_one(sql, params, readonly=True)


# -- writes (writer pool only) ---------------------------------------------
def execute(sql: str, params: tuple | dict | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_many(sql: str, params_list: list) -> int:
    if not params_list:
        return 0
    with cursor() as cur:
        cur.executemany(sql, params_list)
        return cur.rowcount
