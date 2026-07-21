"""
core.universe
=============
The set of tickers a mode ranks over. One universe feeds every strategy;
strategies self-exclude (return a non-rankable Signal) wherever they lack the
data they need, so the universe need not know which strategy will consume it.

Point-in-time caveat -- the same one the snapshot strategies carry. `as_of` is
accepted for interface symmetry, but this returns the CURRENT active universe,
which is correct for as_of ~ today (select mode). A historical as_of would need
survivorship-aware reconstruction (which symbols were listed and active then) --
that's the layer backtest mode will require, and it is not built yet. Running
select at a past date therefore uses today's membership; fine for a live pick,
not yet honest for a historical backtest.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .context import Context


def universe(
    ctx: Context,
    as_of: Optional[date] = None,
    exchange: Optional[str] = None,
    sector: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Active common-stock tickers that have a fundamentals row, market-cap
    ordered (so --limit keeps the largest names, the sane default for a smoke
    run)."""
    where = [
        "s.is_active = true",
        "s.delisted_on IS NULL",
        "(s.type ILIKE 'common stock' OR s.type IS NULL)",
    ]
    params: list = []
    if exchange:
        where.append("s.exchange_code = %s")
        params.append(exchange)
    if sector:
        where.append("f.sector = %s")
        params.append(sector)
    if min_market_cap:
        where.append("f.market_cap >= %s")
        params.append(min_market_cap)

    sql = f"""
        SELECT s.ticker
          FROM symbols s
          JOIN fundamentals f ON f.ticker = s.ticker
         WHERE {' AND '.join(where)}
      ORDER BY f.market_cap DESC NULLS LAST
    """
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))

    return [r["ticker"] for r in ctx.fetch_all(sql, tuple(params))]
