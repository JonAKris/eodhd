"""
strategies.institutional_flow
==============================
The first concrete strategy. It wraps `inst_flow_ticker` -- it does not
recompute anything. All the flow math (per-holder delta labelling, the
latest-filing anchor, the top-N cap accounting) already lives in the
materialized view built by 01_metrics_views.sql, and duplicating it in Python
would be two implementations drifting apart. The strategy's whole job is to
(1) read the rollup, (2) apply the data-validity floors, (3) enforce the
no-look-ahead as_of contract, and (4) carry the view's bias caveats forward in
structured form so a ranker can use the number honestly.

The signal is `net_change_pct` -- the same figure the newsletter ranks
inst_accumulation / inst_distribution on.

Why this signal is prospective-only, enforced in code
-----------------------------------------------------
`institutional_holders` is a *current-snapshot* source: it lists each ticker's
present holders with EODHD's own per-holder change. It does not contain history.
You therefore cannot ask "what did net_change_pct look like six months ago" and
get an honest answer from a single snapshot -- that reading only exists once you
have *banked* the snapshot as a dated vintage.

So `evaluate` gates on `observed_at`: if the only vintage we hold was observed
*after* the requested as_of, the honest answer is "we did not know this then,"
returned as an excluded Signal -- never a reconstruction. With just the live
view (one vintage, observed now), every historical backtest date correctly
returns None, which is precisely the truth: this signal cannot be backtested
retrospectively, only accumulated forward. When a vintage history table exists,
only the relation name in `_VINTAGE_SQL` changes; the contract does not.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..core.contract import Signal
from ..core.context import Context


# Reads the current rollup as a single vintage. To generalize to a banked
# history, point this at the vintage table and add
#   AND observed_at::date <= %(as_of)s  ORDER BY observed_at DESC LIMIT 1
# The Python gate below already models that selection for the single-vintage case.
_VINTAGE_SQL = """
    SELECT ticker,
           latest_filing,
           observed_at,
           top_n_holders,
           top_n_at_cap,
           holders_at_latest,
           holders_lagging,
           net_change_shares,
           prior_shares_at_latest,
           net_change_pct,
           top_n_pct_of_shares_out,
           largest_holder_pct,
           n_added, n_trimmed, n_initiated, n_unchanged
      FROM inst_flow_ticker
     WHERE ticker = %s
"""


class InstitutionalFlowStrategy:
    """Net institutional share-count change at the latest filing date, as a
    per-ticker signal. Conforms to core.contract.Strategy."""

    name = "institutional_flow"

    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        rows = ctx.fetch_all(_VINTAGE_SQL, (ticker,))
        if not rows:
            return Signal.excluded(as_of, "not_covered")
        row = rows[0]

        # ---- 1. no-look-ahead gate --------------------------------------
        observed = _as_date(row["observed_at"])
        if observed is None or observed > as_of:
            # We hold no vintage observed on or before as_of. Refuse to invent
            # one. This is what makes the signal prospective-only in practice.
            return Signal.excluded(
                as_of, "no_vintage_as_of",
                earliest_observed=observed.isoformat() if observed else None,
            )

        # ---- 2. data-validity floors ------------------------------------
        # net_change_pct is NULL when there are too few current filers for the
        # delta to mean anything (the view's own guard); treat as no reading.
        net = _f(row["net_change_pct"])
        if net is None:
            return Signal.excluded(as_of, "net_change_null")

        floors = ctx.floors
        top_n_holders = int(row["top_n_holders"] or 0)
        holders_at_latest = int(row["holders_at_latest"] or 0)
        prior_shares = _f(row["prior_shares_at_latest"]) or 0.0

        if top_n_holders < floors.min_holders:
            return Signal.excluded(as_of, "below_min_holders",
                                   top_n_holders=top_n_holders)
        if holders_at_latest < floors.min_holders_at_latest:
            return Signal.excluded(as_of, "below_min_holders_at_latest",
                                   holders_at_latest=holders_at_latest)
        if prior_shares < floors.min_prior_shares:
            # The +1342%-on-a-23k-base case. The pct is real arithmetic but
            # meaningless as a signal; exclude rather than rank on noise.
            return Signal.excluded(as_of, "below_min_prior_shares",
                                   prior_shares_at_latest=prior_shares)

        # ---- 3. carry the caveats, do not launder them ------------------
        at_cap = bool(row["top_n_at_cap"])
        # The view's bias_note in structured form: at the cap, exits and
        # rank-20 drop-offs delete only *negative* flow, so a negative reading
        # is a lower bound on selling, not a point estimate.
        lower_bound = at_cap and net < 0

        flags = {
            "top_n_at_cap": at_cap,
            "reading_is_lower_bound": lower_bound,
            "holders_at_latest": holders_at_latest,
            "holders_lagging": int(row["holders_lagging"] or 0),
            "stale_days": (as_of - observed).days,
            "latest_filing": _iso(row["latest_filing"]),
        }
        detail = {
            "net_change_shares": _f(row["net_change_shares"]),
            "prior_shares_at_latest": prior_shares,
            "net_change_pct": net,
            "top_n_holders": top_n_holders,
            "top_n_pct_of_shares_out": _f(row["top_n_pct_of_shares_out"]),
            "largest_holder_pct": _f(row["largest_holder_pct"]),
            "n_added": int(row["n_added"] or 0),
            "n_trimmed": int(row["n_trimmed"] or 0),
            "n_initiated": int(row["n_initiated"] or 0),
            "n_unchanged": int(row["n_unchanged"] or 0),
            "observed_at": observed.isoformat(),
        }
        return Signal(value=net, as_of=as_of, flags=flags, detail=detail)


# ---------------------------------------------------------------------------
# small coercion helpers -- psycopg returns numeric as Decimal, dates as date
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
