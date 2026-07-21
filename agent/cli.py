"""
agent.cli
=========
Run the agent. Currently one mode: select (rank the universe by one strategy).

    python -m agent.cli select --strategy ssg --top 25
    python -m agent.cli select --strategy momentum --limit 500
    python -m agent.cli select --strategy insider --ascending --top 20   # net sellers
    python -m agent.cli select --strategy value --sector Technology --out value.csv

Run from the repo root (so ssg_screener.py is importable and DATABASE_URL /
PG_* env vars are picked up by Context.from_dsn()).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime
from typing import Optional

from .core.context import Context
from .core.registry import default_registry
from .core.universe import universe
from .modes.select import run_select, Ranked

log = logging.getLogger("agent")


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _as_of(s: Optional[str]) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else date.today()


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4f}"


# flags worth surfacing in the console note, across strategies
_NOTE_KEYS = ("zone", "is_buy", "direction", "reading_is_lower_bound",
              "negative_earnings", "top_n_at_cap", "filing_gate")


def _note(sig) -> str:
    bits = []
    for k in _NOTE_KEYS:
        v = sig.flags.get(k)
        if v not in (None, False):
            bits.append(f"{k}={v}")
    return "  ".join(bits)


def _print_top(ranked: list[Ranked], top: int, name: str, as_of: date) -> None:
    shown = min(top, len(ranked))
    print(f"\n{name} — top {shown} of {len(ranked)} rankable, as of {as_of}\n")
    print(f"{'#':>4}  {'TICKER':<12}{'VALUE':>16}  NOTES")
    print("-" * 74)
    for r in ranked[:top]:
        print(f"{r.rank:>4}  {r.ticker:<12}{_fmt(r.value):>16}  {_note(r.signal)}")


def _write_csv(path: str, ranked: list[Ranked]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "ticker", "value", "flags"])
        for r in ranked:
            w.writerow([r.rank, r.ticker, r.value,
                        json.dumps(r.signal.flags, default=str)])


# ---------------------------------------------------------------------------
# select command
# ---------------------------------------------------------------------------
def cmd_select(args: argparse.Namespace) -> int:
    ctx = Context.from_dsn()
    reg = default_registry()
    try:
        strat = reg.get(args.strategy)
    except KeyError as e:
        log.error("%s", e)
        return 2

    as_of = _as_of(args.as_of)
    tickers = universe(ctx, as_of=as_of, exchange=args.exchange,
                       sector=args.sector, min_market_cap=args.min_market_cap,
                       limit=args.limit)
    log.info("universe=%d | strategy=%s | as_of=%s", len(tickers), strat.name, as_of)
    if not tickers:
        log.warning("empty universe; nothing to rank")
        return 0

    ranked, stats = run_select(ctx, strat, tickers, as_of,
                               ascending=args.ascending, progress=500)
    log.info("rankable=%d excluded=%d errors=%d",
             stats["rankable"], stats["excluded"], stats["errors"])

    _print_top(ranked, args.top, strat.name, as_of)
    if args.out:
        _write_csv(args.out, ranked)
        log.info("wrote %d rows -> %s", len(ranked), args.out)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("select", help="rank the universe by one strategy")
    s.add_argument("--strategy", required=True, help="registered strategy name")
    s.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD (default today)")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--ascending", action="store_true",
                   help="rank low->high (e.g. net sellers / distribution)")
    s.add_argument("--exchange")
    s.add_argument("--sector")
    s.add_argument("--min-market-cap", type=float, default=None)
    s.add_argument("--limit", type=int, default=None,
                   help="cap universe to the N largest (for a fast smoke run)")
    s.add_argument("--out", help="also write full ranking to this CSV")
    s.set_defaults(func=cmd_select)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
