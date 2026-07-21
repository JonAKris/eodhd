"""
strategies.insider
==================
The mirror image of institutional_flow, and the reason it is worth writing
second: it stresses the same contract from the opposite direction.

institutional_flow is a *snapshot* signal -- prospective-only, returns None for
any historical as_of. insider is an *event* signal: `insider_transactions` is a
stream keyed on transaction date, so a trailing-window net figure can be
computed as of any past date. Every historical as_of should return a real
value. If one `Signal`/`Strategy`/`Context` carries both cleanly, the interface
is proven on both shapes.

The signal is net open-market insider value over a trailing window --
buy_value minus sell_value, codes P and S only (A/M/G are grants, option
exercises and gifts: compensation, not conviction). Positive = net buying.

The look-ahead trap this signal must avoid
------------------------------------------
A Form 4 transaction dated day T is NOT public on day T. The SEC requires the
filing within two business days, so the market learns of it at the *filing*
date, T+1 or T+2. A backtest that windows on `transaction_date <= as_of` counts
transactions that had not yet been disclosed as of as_of -- textbook
look-ahead. So the strategy gates on a per-row knowability date:

    COALESCE(report_date, transaction_date + filing_lag_days) <= as_of

-- the actual SEC filing date (`report_date`) where the ingest has captured it,
and a conservative lag proxy where it hasn't. This uses the exact date wherever
it exists WITHOUT dropping rows that predate the report_date backfill (those
just fall back to the proxy), so precision rises as the column fills in with no
coverage cliff. `flags['filing_gate']` records which sources were in play, so
the approximation is never silent.

Note the strategy deliberately does NOT join `fundamentals` to normalize by
market cap: that table is a latest snapshot, and joining it would sneak a
snapshot look-ahead back into an otherwise clean event signal. Cross-sectional
normalization is a consumer concern, to be done with point-in-time market cap
where available -- not baked in here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from ..core.contract import Signal
from ..core.context import Context

_UNSET = object()


class InsiderStrategy:
    """Net open-market insider value (buys minus sells) over a trailing window,
    as a per-ticker signal. Conforms to core.contract.Strategy.

    Parameters are strategy-specific and passed at construction; shared
    data-validity floors still come from ctx.floors. This is the pattern:
    strategy-private knobs are constructor args, cross-strategy floors live in
    Context.

    Reuse one instance across tickers -- the filing-date column is detected once
    and cached on the instance.
    """

    name = "insider"

    def __init__(
        self,
        window_days: int = 30,
        min_txns: int = 2,        # a single transaction is noise (newsletter's HAVING >= 2)
        filing_lag_days: int = 4, # conservative cover for the 2-business-day Form 4
                                  # deadline across a weekend; the proxy when report_date is null
    ) -> None:
        self.window_days = window_days
        self.min_txns = min_txns
        self.filing_lag_days = filing_lag_days
        self._has_report_date = _UNSET  # resolved once on first evaluate()

    # -- schema detection (once per instance) -----------------------------
    def _report_date_present(self, ctx: Context) -> bool:
        """True if insider_transactions has a report_date column to gate on."""
        if self._has_report_date is not _UNSET:
            return self._has_report_date
        rows = ctx.fetch_all(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'insider_transactions' "
            "AND column_name = 'report_date'",
            None,
        )
        self._has_report_date = bool(rows)
        return self._has_report_date

    # -- the contract -----------------------------------------------------
    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        window_start = as_of - timedelta(days=self.window_days)
        lag = int(self.filing_lag_days)  # controlled int, safe to inline

        if self._report_date_present(ctx):
            # Per-row knowability: exact filing date where present, conservative
            # proxy where report_date is still null (rows not yet re-pulled).
            knowable = (f"COALESCE(report_date, "
                        f"transaction_date + make_interval(days => {lag}))")
            filing_gate = f"report_date|proxy:{lag}d"
        else:
            # No report_date column yet: proxy every row.
            knowable = f"(transaction_date + make_interval(days => {lag}))"
            filing_gate = f"proxy:{lag}d"

        sql = f"""
            SELECT count(*)                                            AS n_txns,
                   count(*) FILTER (WHERE transaction_code = 'P')      AS n_buys,
                   count(*) FILTER (WHERE transaction_code = 'S')      AS n_sells,
                   count(DISTINCT owner_name)                          AS n_insiders,
                   COALESCE(sum(value) FILTER (WHERE transaction_code = 'P'), 0) AS buy_value,
                   COALESCE(sum(value) FILTER (WHERE transaction_code = 'S'), 0) AS sell_value,
                   min(transaction_date)                              AS earliest,
                   max(transaction_date)                              AS latest
              FROM insider_transactions
             WHERE ticker = %s
               AND transaction_code IN ('P', 'S')
               AND value IS NOT NULL
               AND transaction_date >  %s
               AND transaction_date <= %s
               AND {knowable} <= %s
        """
        params = (ticker, window_start, as_of, as_of)

        rows = ctx.fetch_all(sql, params)
        agg = rows[0] if rows else {}
        n_txns = int(agg.get("n_txns") or 0)

        if n_txns == 0:
            return Signal.excluded(as_of, "no_activity", filing_gate=filing_gate)
        if n_txns < self.min_txns:
            return Signal.excluded(as_of, "below_min_txns",
                                   n_txns=n_txns, filing_gate=filing_gate)

        buy_value = _f(agg.get("buy_value")) or 0.0
        sell_value = _f(agg.get("sell_value")) or 0.0
        net_value = buy_value - sell_value  # 0.0 is a real flat reading, not absence

        if net_value > 0:
            direction = "net_buying"
        elif net_value < 0:
            direction = "net_selling"
        else:
            direction = "net_flat"

        flags = {
            "filing_gate": filing_gate,
            "window_days": self.window_days,
            "direction": direction,
            # The asymmetry insider research turns on: purchases are the strong
            # signal; sales happen for liquidity and diversification reasons that
            # have nothing to do with outlook. Carry it, don't launder it.
            "sales_are_noisier": net_value < 0,
            "n_buys": int(agg.get("n_buys") or 0),
            "n_sells": int(agg.get("n_sells") or 0),
            "n_insiders": int(agg.get("n_insiders") or 0),
        }
        detail = {
            "buy_value": buy_value,
            "sell_value": sell_value,
            "net_value": net_value,
            "n_txns": n_txns,
            "n_buys": flags["n_buys"],
            "n_sells": flags["n_sells"],
            "n_insiders": flags["n_insiders"],
            "earliest": _iso(agg.get("earliest")),
            "latest": _iso(agg.get("latest")),
            "window_start": window_start.isoformat(),
            "as_of": as_of.isoformat(),
        }
        return Signal(value=net_value, as_of=as_of, flags=flags, detail=detail)


# ---------------------------------------------------------------------------
# coercion helpers (shared shape with institutional_flow)
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
