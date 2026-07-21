"""
strategies.value
================
Earnings yield (trailing-twelve-month EPS / price) as a value signal. Higher =
cheaper. A standard, minimal value factor; book-to-price and sales-to-price are
natural siblings to add behind the same interface later.

Point-in-time, and for a subtle reason worth stating. The obvious value source
is the `fundamentals` header (`f.pe_ratio`, `f.eps`) -- but that is a latest
snapshot, so using it would make value prospective-only like the SSG. Instead
this builds TTM EPS from `earnings_history`, whose `date` is the *announcement*
date: an earnings figure is public as of its announcement, so summing the four
most recent quarters with announcement date <= as_of is knowability-honest with
no filing-date guesswork. Divided by the adjusted close as of as_of, the result
is a real earnings yield observable at any past date -- backtestable, like
momentum.

Negative TTM EPS yields a negative signal (a real reading: the company lost
money). It is flagged rather than dropped, because "loss-making" is information a
value ranker will want to handle explicitly, not silently.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..core.contract import Signal
from ..core.context import Context

_EPS_SQL = """
    SELECT date, eps_actual
      FROM earnings_history
     WHERE ticker = %s
       AND date <= %s
       AND eps_actual IS NOT NULL
  ORDER BY date DESC
     LIMIT 4
"""

_PRICE_SQL = """
    SELECT adjusted_close
      FROM eod_prices
     WHERE ticker = %s
       AND date <= %s
       AND adjusted_close IS NOT NULL
       AND adjusted_close > 0
  ORDER BY date DESC
     LIMIT 1
"""


class ValueStrategy:
    """Trailing earnings yield (TTM EPS / price) as a per-ticker signal.
    Conforms to core.contract.Strategy."""

    name = "value"

    def __init__(self, min_quarters: int = 4) -> None:
        # Require a full four quarters for a clean TTM; fewer is a partial year
        # that would understate or overstate the yield.
        self.min_quarters = min_quarters

    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        eps_rows = ctx.fetch_all(_EPS_SQL, (ticker, as_of))
        eps_vals = [_f(r["eps_actual"]) for r in eps_rows]
        eps_vals = [e for e in eps_vals if e is not None]
        if len(eps_vals) < self.min_quarters:
            return Signal.excluded(as_of, "insufficient_eps_history",
                                   quarters=len(eps_vals))
        ttm_eps = sum(eps_vals[: self.min_quarters])

        price_rows = ctx.fetch_all(_PRICE_SQL, (ticker, as_of))
        price = _f(price_rows[0]["adjusted_close"]) if price_rows else None
        if not price:
            return Signal.excluded(as_of, "no_price")

        earnings_yield = ttm_eps / price

        flags = {
            "negative_earnings": ttm_eps < 0,
            "quarters_used": self.min_quarters,
            "latest_earnings_date": _iso(eps_rows[0]["date"]) if eps_rows else None,
        }
        detail = {
            "ttm_eps": ttm_eps,
            "price": price,
            "earnings_yield": earnings_yield,
            "as_of": as_of.isoformat(),
        }
        return Signal(value=earnings_yield, as_of=as_of, flags=flags, detail=detail)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)).date()
    except ValueError:
        return None


def _iso(v) -> Optional[str]:
    d = _as_date(v)
    return d.isoformat() if d else None
