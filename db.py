"""
db.py
-----
Shared psycopg connection pool and a handful of row helpers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=1,
            max_size=settings.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


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
