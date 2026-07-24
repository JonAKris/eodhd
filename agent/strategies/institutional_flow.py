"""
strategies.institutional_flow
==============================
Wraps the holder-flow rollup -- it does not recompute anything. All the flow
math (per-holder delta labelling, the latest-filing anchor, the top-N cap
accounting) lives in 01_metrics_views.sql; duplicating it in Python would be two
implementations drifting apart. The strategy's job is to (1) read the right
vintage of the rollup, (2) apply the data-validity floors, (3) enforce the
no-look-ahead as_of contract, and (4) carry the view's bias caveats forward in
structured form so a ranker can use the number honestly.

The signal is `net_change_pct` -- the same figure the newsletter ranks
inst_accumulation / inst_distribution on.

Two sources, one behaviour
--------------------------
`institutional_holders` is a *current-snapshot* source with no history, so the
live rollup `inst_flow_ticker` can only answer "what is true now". The vintage
layer (flow_vintages.sql) fixes that by appending a dated copy of the rollup on
every refresh, into `inst_flow_vintage`, stamped with `banked_at`.

  * When banked vintages exist, the strategy reads the latest vintage with
    `banked_at <= as_of` -- the most recent reading the system had actually
    stored by that date. A historical as_of now returns a REAL value, as far
    back as the first bank. This is the promotion from prospective-only to
    backtestable; it reaches back only to when banking began, because we cannot
    query a vintage we never observed.
  * Before any vintages are banked (table absent or empty), it falls back to
    the live rollup and the original prospective-only gate on `observed_at`: a
    historical as_of returns None, because the single live snapshot is all we
    have. This keeps live `select` working the moment the strategy ships,
    before the first bank.

Either way the knowability gate is honest -- banked_at for vintages (when it
entered our store), observed_at for the live fallback (when the holders were
observed) -- and never reconstructs a reading it didn't hold.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..core.contract import Signal
from ..core.context import Context

_UNSET = object()

# Shared column projection, so the floors/flags code downstream is identical
# regardless of source. The banked source adds banked_at and the as_of filter.
_COLS = """ticker, latest_filing, observed_at,
           top_n_holders, top_n_at_cap, holders_at_latest, holders_lagging,
           net_change_shares, prior_shares_at_latest, net_change_pct,
           top_n_pct_of_shares_out, largest_holder_pct,
           n_added, n_trimmed, n_initiated, n_unchanged"""

# Live fallback: the single current vintage.
_LIVE_SQL = f"SELECT {_COLS} FROM inst_flow_ticker WHERE ticker = %s"

# Point-in-time: the latest vintage the system had stored by as_of.
_BANKED_SQL = f"""
    SELECT {_COLS}, banked_at
      FROM inst_flow_vintage
     WHERE ticker = %s
       AND banked_at <= %s
  ORDER BY banked_at DESC
     LIMIT 1
"""

_ANY_VINTAGE_SQL = "SELECT 1 FROM inst_flow_vintage WHERE ticker = %s LIMIT 1"


class InstitutionalFlowStrategy:
    """Net institutional share-count change at the latest filing date, as a
    per-ticker signal. Conforms to core.contract.Strategy.

    Reuse one instance across tickers -- vintage-table availability is detected
    once and cached."""

    name = "institutional_flow"

    def __init__(self) -> None:
        self._use_v = _UNSET  # resolved on first evaluate()

    # -- source selection (once per instance) -----------------------------
    def _use_vintage(self, ctx: Context) -> bool:
        """True iff inst_flow_vintage exists AND holds at least one row. Empty
        or absent -> fall back to the live rollup, so a freshly-migrated but
        not-yet-banked database still serves live picks."""
        if self._use_v is not _UNSET:
            return self._use_v
        exists = ctx.fetch_all(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'inst_flow_vintage'", None)
        if not exists:
            self._use_v = False
        else:
            self._use_v = bool(ctx.fetch_all("SELECT 1 FROM inst_flow_vintage LIMIT 1", None))
        return self._use_v

    # -- the contract -----------------------------------------------------
    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        if self._use_vintage(ctx):
            rows = ctx.fetch_all(_BANKED_SQL, (ticker, as_of))
            if not rows:
                # No vintage stored by as_of. Distinguish "we never covered this
                # ticker" from "we hadn't banked one yet by that date".
                covered = ctx.fetch_all(_ANY_VINTAGE_SQL, (ticker,))
                return Signal.excluded(as_of,
                                       "no_vintage_as_of" if covered else "not_covered")
            row = rows[0]
            know = _as_date(row["banked_at"])
            know_kind = "banked_at"
        else:
            rows = ctx.fetch_all(_LIVE_SQL, (ticker,))
            if not rows:
                return Signal.excluded(as_of, "not_covered")
            row = rows[0]
            know = _as_date(row["observed_at"])
            know_kind = "observed_at"
            # Live fallback keeps the original prospective-only gate: the one
            # snapshot we hold is only knowable from when it was observed.
            if know is None or know > as_of:
                return Signal.excluded(
                    as_of, "no_vintage_as_of",
                    earliest_observed=know.isoformat() if know else None)

        # ---- data-validity floors ---------------------------------------
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

        # ---- carry the caveats, do not launder them ---------------------
        at_cap = bool(row["top_n_at_cap"])
        # The view's bias_note in structured form: at the cap, exits and rank-20
        # drop-offs delete only *negative* flow, so a negative reading is a lower
        # bound on selling, not a point estimate.
        lower_bound = at_cap and net < 0

        observed = _as_date(row["observed_at"])
        flags = {
            "top_n_at_cap": at_cap,
            "reading_is_lower_bound": lower_bound,
            "holders_at_latest": holders_at_latest,
            "holders_lagging": int(row["holders_lagging"] or 0),
            "knowability": know_kind,      # banked_at (PIT) or observed_at (live)
            "stale_days": (as_of - know).days if know else None,
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
            "observed_at": observed.isoformat() if observed else None,
        }
        if know_kind == "banked_at":
            detail["banked_at"] = know.isoformat()
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
