"""
modes.select
============
Rank a universe by one strategy as of a date. This is the live-pick mode -- the
generalization of ssg_screener's ranked CSV to any registered strategy.

The runner is deliberately dumb and honest: evaluate every ticker, keep the ones
that come back rankable (value is not None), sort. Non-rankable signals are
counted, not ranked -- an excluded signal means "no reading", never "zero", so
it must not sink to the bottom of the ranking as if it were the worst name.

run_select takes an explicit ticker list rather than loading the universe
itself, so it is testable offline and so the caller controls universe scope.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..core.contract import Signal, Strategy
from ..core.context import Context

log = logging.getLogger("agent.select")


@dataclass
class Ranked:
    rank: int
    ticker: str
    value: float
    signal: Signal


def run_select(
    ctx: Context,
    strategy: Strategy,
    tickers: list[str],
    as_of: date,
    ascending: bool = False,
    on_error: str = "skip",   # "skip" (default) or "raise"
    progress: Optional[int] = None,
) -> tuple[list[Ranked], dict]:
    """Evaluate `strategy` over `tickers` as of `as_of`; return the rankable
    ones sorted (descending by default), plus a stats dict.

    ascending=True ranks low->high, for signals whose interesting tail is the
    bottom (net selling, institutional distribution)."""
    scored: list[tuple[str, float, Signal]] = []
    excluded = errors = 0
    n = len(tickers)

    for i, t in enumerate(tickers, 1):
        try:
            sig = strategy.evaluate(t, as_of, ctx)
        except Exception:
            errors += 1
            if on_error == "raise":
                raise
            log.warning("evaluate failed for %s (skipped)", t, exc_info=False)
            continue
        if sig.is_rankable:
            scored.append((t, float(sig.value), sig))
        else:
            excluded += 1
        if progress and i % progress == 0:
            log.info("  %d/%d evaluated (%d rankable)", i, n, len(scored))

    scored.sort(key=lambda r: r[1], reverse=not ascending)
    ranked = [Ranked(i, t, v, s) for i, (t, v, s) in enumerate(scored, 1)]
    stats = {"universe": n, "rankable": len(ranked),
             "excluded": excluded, "errors": errors}
    return ranked, stats
