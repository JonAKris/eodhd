"""
strategies.ssg
==============
The third shape, and the last one the contract hadn't seen: a rich screener.
institutional_flow and insider each emit a bare scalar; the SSG emits a full
five-section study *and* a rankable number. This is what `Signal.detail` was
designed to carry -- the scalar goes in `.value`, the whole study goes in
`.detail`, and no separate `Screener` subtype is needed.

The signal is `SSGResult.total_return` -- the projected five-year total return,
the figure you would rank names on. The study (quality gates, focus-forecast
methods, P/E history, price zones, buy verdict) rides in `.detail`.

Reuse, not reimplementation
---------------------------
ssg_screener.py already computes all of this and has its own passing self-test.
Transcribing 700 lines of SSG arithmetic into this package would only introduce
bugs. So this strategy *reuses* `build_ssg` verbatim and injects Context's data
access into it: the loaders in ssg_screener reference a module-level `fetch_all`,
so reassigning `ssg_screener.fetch_all = ctx.fetch_all` redirects every query
through the shared Context without touching the math. The SSG's tested logic
stays authoritative; this file only adapts its I/O to the contract.

Live-only, honestly gated
-------------------------
As written, ssg_screener reads latest-snapshot fundamentals (the `fundamentals`
header: market cap, ROE, current EPS, yield) and windows prices off
`date.today()`. That makes it valid for "now", not for a historical as_of --
the same snapshot look-ahead institutional_flow has. So this strategy gates
prospective-only: a historical as_of returns `no_pit_fundamentals` rather than a
reconstruction. Making the SSG backtestable is the same future work as the
vintage layer -- lag the fundamentals to filing date and reconstruct price
windows to as_of -- and needs no change to the contract when it lands.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Callable, Optional

from ..core.contract import Signal
from ..core.context import Context

# Same columns ssg_screener.load_universe selects, for one ticker. build_ssg
# reads name/sector/market_cap/eps/return_on_equity/dividend_share/dividend_yield
# off this row; the rest are selected for parity and future use.
_HEADER_SQL = """
    SELECT s.ticker, s.name, s.exchange_code, s.currency,
           f.sector, f.industry, f.market_cap, f.pe_ratio, f.eps,
           f.dividend_share, f.dividend_yield, f.return_on_equity,
           f.profit_margin, f.wall_street_target_price
      FROM symbols s
      JOIN fundamentals f ON f.ticker = s.ticker
     WHERE s.ticker = %s
     LIMIT 1
"""


class SSGStrategy:
    """BetterInvesting/NAIC Stock Selection Guide as a strategy. Signal is the
    projected five-year total return; the full study is in Signal.detail.
    Conforms to core.contract.Strategy.

    `build_ssg` may be injected (for offline testing); left None, it is resolved
    from ssg_screener.py at first use and wired to Context for data access.
    """

    name = "ssg"

    def __init__(self, build_ssg: Optional[Callable[[dict], object]] = None) -> None:
        self._build_fn = build_ssg

    def evaluate(self, ticker: str, as_of: date, ctx: Context) -> Signal:
        # Prospective-only: latest-snapshot fundamentals + today()-windowed
        # prices are valid for now, not for a past date. Refuse to reconstruct.
        if as_of < ctx.today:
            return Signal.excluded(
                as_of, "no_pit_fundamentals",
                note="SSG uses latest-snapshot fundamentals; historical as_of "
                     "needs point-in-time reconstruction",
            )

        row = self._header(ticker, ctx)
        if not row:
            return Signal.excluded(as_of, "not_covered")

        build = self._resolve_build(ctx)
        result = build(row)
        detail = asdict(result)

        flags = {
            "quality_pass": bool(getattr(result, "quality_pass", False)),
            "is_buy": bool(getattr(result, "is_buy", False)),
            "zone": getattr(result, "zone", None),
            "updown_ratio": getattr(result, "updown_ratio", None),
            "fc_eps_method": getattr(result, "fc_eps_method", None),
            "fc_backtest_err": getattr(result, "fc_backtest_err", None),
            "reasons": list(getattr(result, "reasons", [])),
        }

        total_return = getattr(result, "total_return", None)
        if total_return is None:
            # Computed but no five-year projection (failed a gate, or price zones
            # unavailable). Not rankable -- but the study still rides in detail so
            # a consumer can see *why* rather than getting a bare None.
            return Signal(value=None, as_of=as_of,
                          flags={"reason": "no_projection", **flags}, detail=detail)

        return Signal(value=float(total_return), as_of=as_of, flags=flags, detail=detail)

    # -- helpers ----------------------------------------------------------
    def _header(self, ticker: str, ctx: Context) -> Optional[dict]:
        rows = ctx.fetch_all(_HEADER_SQL, (ticker,))
        return rows[0] if rows else None

    def _resolve_build(self, ctx: Context) -> Callable[[dict], object]:
        if self._build_fn is not None:
            return self._build_fn
        try:
            import ssg_screener  # lives at the repo root; import lazily
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "SSGStrategy needs ssg_screener.py importable. Run from the repo "
                f"root (e.g. cd ~/eodhd) or add it to PYTHONPATH. Original: {e}"
            )
        # Inject Context's data access so the SSG's loaders query through the
        # shared connection instead of opening their own. Math is untouched.
        ssg_screener.fetch_all = ctx.fetch_all
        return ssg_screener.build_ssg
