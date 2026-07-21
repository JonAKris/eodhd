"""
strategies.momentum
===================
Classic 12-1 price momentum (Jegadeesh-Titman): the cumulative return over the
trailing twelve months, *skipping the most recent month* to sidestep short-term
reversal. Signal is that return; positive = strong recent performer.

Point-in-time by construction. `eod_prices.adjusted_close` is keyed on the
trading date and adjusted for splits/dividends, so a return between two past
dates is exactly what was observable then -- nothing to reconstruct, no snapshot
to lag. Unlike institutional_flow or the SSG, momentum returns a real value for
any historical as_of, which is what makes it (and the whole price-factor family)
backtestable over existing history. This is the retrospective shape the contract
should carry as cleanly as it carries the prospective one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from ..core.contract import Signal
from ..core.context import Context

_PRICE_SQL = """
    SELECT date, adjusted_close
      FROM eod_prices
     WHERE ticker = %s
       AND date <= %s
       AND date >= %s
       AND adjusted_close IS NOT NULL
       AND adjusted_close > 0
  ORDER BY date ASC
"""


class MomentumStrategy:
    """Trailing lookback-minus-skip price momentum as a per-ticker signal.
    Conforms to core.contract.Strategy.

    Strategy-specific knobs are constructor args (the established pattern);
    there are no shared validity floors for a pure price factor.
    """

    name = "momentum"

    def __init__(
        self,
        lookback_months: int = 12,
        skip_months: int = 1,
        max_gap_days: int = 10,  # a leg's nearest price must be within this of its
                                 # target date, else the window is broken (halt/thin)
    ) -> None:
        self.lookback_months = lookback_months
        self.skip_months = skip_months
        self.max_gap_days = max_gap_days

    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        recent_target = _months_before(as_of, self.skip_months)
        old_target = _months_before(as_of, self.lookback_months)

        # one query for the whole window; pick each leg in Python
        buffer_start = old_target - timedelta(days=self.max_gap_days + 5)
        rows = ctx.fetch_all(_PRICE_SQL, (ticker, as_of, buffer_start))
        series = [(_as_date(r["date"]), _f(r["adjusted_close"])) for r in rows]
        series = [(d, p) for d, p in series if d is not None and p]

        if len(series) < 2:
            return Signal.excluded(as_of, "insufficient_price_history",
                                   n_prices=len(series))

        recent = _nearest_on_or_before(series, recent_target)
        old = _nearest_on_or_before(series, old_target)
        if recent is None or old is None:
            return Signal.excluded(as_of, "insufficient_price_history")

        # staleness guard: a leg priced far before its target means missing data,
        # not a real reading. Refuse rather than compute momentum off a gap.
        recent_gap = (recent_target - recent[0]).days
        old_gap = (old_target - old[0]).days
        if recent_gap > self.max_gap_days or old_gap > self.max_gap_days:
            return Signal.excluded(as_of, "stale_price_window",
                                   recent_gap_days=recent_gap, old_gap_days=old_gap)

        p_recent, p_old = recent[1], old[1]
        mom = p_recent / p_old - 1.0

        flags = {
            "lookback_months": self.lookback_months,
            "skip_months": self.skip_months,
            "recent_price_date": recent[0].isoformat(),
            "old_price_date": old[0].isoformat(),
        }
        detail = {
            "p_recent": p_recent, "p_old": p_old,
            "recent_target": recent_target.isoformat(),
            "old_target": old_target.isoformat(),
            "as_of": as_of.isoformat(),
        }
        return Signal(value=mom, as_of=as_of, flags=flags, detail=detail)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _months_before(d: date, months: int) -> date:
    """Calendar-correct month subtraction, clamped to day 28 to dodge
    month-length edge cases."""
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, 28))


def _nearest_on_or_before(series, target):
    """Last (date, price) in an ascending series with date <= target."""
    chosen = None
    for d, p in series:
        if d <= target:
            chosen = (d, p)
        else:
            break
    return chosen


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
