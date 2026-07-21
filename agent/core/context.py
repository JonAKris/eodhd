"""
core.context
============
The shared handle every strategy is given at call time. It exists so that no
strategy ever opens its own database connection or hardcodes a universe or a
threshold -- the three things that made the original programs impossible to
compose. `Context` carries data access and the validity floors; a future
revision adds the universe layer and the vintage store described below.

Floors: single source of truth
-------------------------------
The newsletter's `newsletter_config` table already defines the data-validity
floors (`min_prior_shares`, `min_holders`, `min_holders_at_latest`, and the
liquidity pair). Those are not editorial cutoffs -- they gate whether a number
is *meaningful*, so they belong with the strategy, not the presentation layer.
`Floors.from_db` reads them from that same table, so the figure that decides a
newsletter section's inclusion and the figure that decides a strategy's signal
validity are one value defined in one place. `top_n` is deliberately NOT pulled
in: it is how many rows the newsletter prints, which is presentation, and the
seam between validity and presentation runs exactly there.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

FetchAll = Callable[[str, Optional[tuple]], list[dict]]


@dataclass(frozen=True)
class Floors:
    """Data-validity gates. A reading that fails any of these is not a weak
    signal; it is not a signal. Defaults mirror `newsletter_config` so the code
    is correct even before a DB read, but `from_db` is the intended source."""

    min_holders: int = 5              # too few holders to rank on
    min_holders_at_latest: int = 4    # net flow needs enough current filers
    min_prior_shares: float = 50000   # denominator floor: change_p hits +1342%
                                      # on a 23k base; below this the pct is noise
    min_price: float = 5.00           # liquidity: drop sub-$5 names
    min_avg_vol_20d: float = 100000   # liquidity: drop microcap volume noise

    @classmethod
    def from_db(cls, ctx: "Context") -> "Floors":
        """Load floors from the newsletter's own `newsletter_config` table.
        Missing keys fall back to the dataclass defaults, so a partial config
        never silently zeroes a gate."""
        rows = ctx.fetch_all("SELECT key, value FROM newsletter_config", None)
        cfg = {r["key"]: r["value"] for r in rows}

        def g(key: str, default):
            v = cfg.get(key)
            return type(default)(v) if v is not None else default

        d = cls()
        return cls(
            min_holders=g("min_holders", d.min_holders),
            min_holders_at_latest=g("min_holders_at_latest", d.min_holders_at_latest),
            min_prior_shares=g("min_prior_shares", d.min_prior_shares),
            min_price=g("min_price", d.min_price),
            min_avg_vol_20d=g("min_avg_vol_20d", d.min_avg_vol_20d),
        )


class Context:
    """Everything a strategy is allowed to touch, handed in rather than reached
    for. Construct once per run and pass to every `evaluate` call.

    Two construction paths:
      * `Context.from_dsn(...)` -- standalone, opens a psycopg connection.
      * `Context(fetch_all=..., floors=...)` -- inject any fetch callable,
        which is how the offline self-test runs the full strategy logic with
        no database at all (the same idiom ssg_screener.py uses).
    """

    def __init__(
        self,
        fetch_all: FetchAll,
        floors: Floors | None = None,
        today: date | None = None,
    ) -> None:
        self._fetch_all = fetch_all
        self.floors = floors or Floors()
        self.today = today or date.today()

    # -- data access ------------------------------------------------------
    def fetch_all(self, sql: str, params: Optional[tuple] = None) -> list[dict]:
        return self._fetch_all(sql, params)

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dsn(cls, dsn: str | None = None, load_floors: bool = True) -> "Context":
        """Open a real connection. DSN precedence: explicit arg, then
        DATABASE_URL, then PG_* environment variables."""
        import psycopg
        from psycopg.rows import dict_row

        resolved = dsn or _dsn_from_env()
        conn = psycopg.connect(resolved, row_factory=dict_row, autocommit=True)

        def fetch_all(sql: str, params: Optional[tuple] = None) -> list[dict]:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

        ctx = cls(fetch_all=fetch_all)
        if load_floors:
            ctx.floors = Floors.from_db(ctx)
        ctx._conn = conn  # keep a reference so it isn't GC'd mid-run
        return ctx


def _dsn_from_env() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return (
        f"host={os.getenv('PG_HOST', 'localhost')} "
        f"port={os.getenv('PG_PORT', '5432')} "
        f"dbname={os.getenv('PG_DB', 'eodhd')} "
        f"user={os.getenv('PG_USER', 'postgres')} "
        f"password={os.getenv('PG_PASSWORD', '')}"
    )
