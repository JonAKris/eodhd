#!/usr/bin/env python3
"""Autonomous Stock Explorer - Main Agent.

Runs the explorer's SQL strategies, cross-references multi-signal tickers, and
hands grounded facts to the LLM for a report.

Merged into the eodhd repo: the old self-contained `database.py` pool is gone;
this now reads through the shared `db.py` (read-only role via config.ro_dsn).
Run from the repo root:  python -m explorer.runner
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

import db
from config import settings
from .llm import LLMInterface
from .sql_strategies import STRATEGIES, validate_params

console = Console()


def _jsonable(v):
    """Coerce a single DB/pandas value to a JSON-native type."""
    if isinstance(v, Decimal):
        v = float(v)
    elif isinstance(v, (datetime, date)):
        return v.isoformat()
    elif hasattr(v, 'item') and not isinstance(v, (str, bytes)):
        try:
            v = v.item()           # numpy scalar -> python scalar
        except Exception:
            return v
    if isinstance(v, float) and math.isnan(v):
        return None                # JSON has no NaN
    return v


def _jsonable_rows(records):
    """Coerce a list of row dicts so persisted data keeps real numbers/ISO dates."""
    return [{k: _jsonable(val) for k, val in rec.items()} for rec in records]


def _fnum(v):
    """Coerce a numeric DB value to a plain float, or None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def build_facts(top_tickers):
    """Deterministically gather facts for the top conviction tickers straight
    from the database -- name, sector, latest price, and a few key metrics.

    No LLM is involved: these are the authoritative identities/numbers the
    report templates verbatim, so the model can never rename a ticker or
    invent a figure. Returns a list of dicts (one per ticker, in rank order).
    """
    if not top_tickers:
        return []

    tickers = [t for t, _ in top_tickers]
    try:
        rows = db.fetch_all_ro(
            """
            SELECT f.ticker, f.name, f.sector, f.industry, f.market_cap,
                   f.pe_ratio, f.return_on_equity, f.profit_margin,
                   f.dividend_yield, f.quarterly_revenue_growth,
                   e.close AS price
            FROM fundamentals f
            LEFT JOIN LATERAL (
                SELECT close FROM eod_prices
                WHERE ticker = f.ticker ORDER BY date DESC LIMIT 1
            ) e ON true
            WHERE f.ticker = ANY(%s)
            """,
            (tickers,),
        )
    except Exception as exc:
        logger.warning(f"Facts lookup failed ({exc}); picks will show tickers only.")
        rows = []

    by_ticker = {r["ticker"]: r for r in rows}
    facts = []
    for ticker, signals in top_tickers:
        r = by_ticker.get(ticker, {})
        facts.append({
            "ticker": ticker,
            "name": r.get("name") or "(name unavailable)",
            "sector": r.get("sector") or "",
            "industry": r.get("industry") or "",
            "signals": list(signals),
            "price": _fnum(r.get("price")),
            "market_cap": _fnum(r.get("market_cap")),
            "pe_ratio": _fnum(r.get("pe_ratio")),
            "return_on_equity": _fnum(r.get("return_on_equity")),
            "profit_margin": _fnum(r.get("profit_margin")),
            "dividend_yield": _fnum(r.get("dividend_yield")),
            "rev_growth": _fnum(r.get("quarterly_revenue_growth")),
        })
    return facts


def _universe_banner():
    """Cheap replacement for the old connector's get_schema/get_table_stats --
    a startup banner and the universe size, via information_schema + a count.
    Returns (n_tables, universe_size)."""
    n_tables = None
    universe_size = None
    try:
        rows = db.fetch_all_ro(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        n_tables = rows[0]["n"] if rows else None
    except Exception as exc:
        logger.warning(f"table count failed ({exc})")
    try:
        row = db.fetch_one_ro("SELECT count(*) AS n FROM fundamentals")
        universe_size = row["n"] if row else None
    except Exception as exc:
        logger.warning(f"universe size failed ({exc})")
    return n_tables, universe_size


class StockExplorer:
    def __init__(self):
        self.llm = LLMInterface()
        self.findings = []
        self.start_time = datetime.now()
        self.universe_size = None

        Path("logs").mkdir(exist_ok=True)
        logger.add("logs/agent_{time}.log", rotation="1 day", retention="7 days")

    def run(self):
        """Main execution"""
        console.rule("[bold blue]🚀 Stock Explorer Agent Starting")
        console.print(f"[dim]Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")

        try:
            # Reads run through the shared read-only pool; nothing to open/close.
            n_tables, self.universe_size = _universe_banner()
            console.print(
                f"[green]✓[/green] Connected via db.py "
                f"({n_tables if n_tables is not None else '?'} tables, "
                f"universe {self.universe_size:,} names)"
                if self.universe_size is not None else
                f"[green]✓[/green] Connected via db.py"
            )

            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
                task = progress.add_task("[cyan]Executing investment strategies...", total=len(STRATEGIES) * 3)

                all_results = []
                for strategy_name, strategy in STRATEGIES.items():
                    for i in range(3):
                        allowed = strategy.get('params', {})
                        params = {k: random.choice(v) for k, v in allowed.items()}
                        # Gate values before they are formatted into SQL.
                        params = validate_params(strategy_name, allowed, params)

                        try:
                            query = strategy['query'].format(**params)
                            rows = db.fetch_all_ro(query)

                            if rows:
                                df = pd.DataFrame(rows)   # rows are dicts -> columns inferred
                                interpretation = self.llm.interpret_results(
                                    f"Strategy: {strategy_name} (variation {i+1})",
                                    df
                                )

                                all_results.append({
                                    'strategy': strategy_name,
                                    'params': params,
                                    'tickers_found': df['ticker'].tolist() if 'ticker' in df.columns else [],
                                    'row_count': len(df),
                                    'interpretation': interpretation,
                                    'top_rows': df.head(15).to_dict('records'),
                                    'timestamp': datetime.now().isoformat()
                                })

                                tickers = df['ticker'].tolist()[:5] if 'ticker' in df.columns else []
                                console.print(f"  [green]✓[/green] {strategy_name} v{i+1}: {len(df)} results - {', '.join(tickers)}")
                            else:
                                console.print(f"  [yellow]○[/yellow] {strategy_name} v{i+1}: no results")

                        except Exception as e:
                            console.print(f"  [red]✗[/red] {strategy_name} v{i+1}: {str(e)[:80]}")
                            logger.error(f"{strategy_name} failed: {e}")

                        progress.advance(task)

            console.print("\n[bold]Cross-referencing multi-signal stocks...[/bold]")
            ticker_signals = {}
            for r in all_results:
                for t in r.get('tickers_found', []):
                    ticker_signals.setdefault(t, []).append(r['strategy'])

            multi_signal = {t: list(set(s)) for t, s in ticker_signals.items() if len(set(s)) >= 2}
            console.print(f"[green]✓[/green] Found {len(multi_signal)} stocks with multiple signals")

            top_tickers = []
            if multi_signal:
                top_tickers = sorted(multi_signal.items(), key=lambda x: len(x[1]), reverse=True)[:10]
                console.print(f"\n[bold]Deep diving top {len(top_tickers)} conviction picks:[/bold]")
                for ticker, signals in top_tickers:
                    console.print(f"  [cyan]🔍 {ticker}[/cyan] - {len(signals)} signals: {', '.join(signals)}")

            # Deterministic facts for the conviction picks (authoritative names/numbers)
            facts = build_facts(top_tickers)

            self.save_results(all_results, multi_signal, top_tickers, facts)
            self.persist_signals(multi_signal, top_tickers)

            console.print("\n[bold]Generating investment report...[/bold]")
            report = self.llm.synthesize_report(facts, all_results, universe_size=self.universe_size)
            self.save_report(report)

            console.rule("[bold green]✅ Exploration Complete!")
            duration = (datetime.now() - self.start_time).total_seconds() / 60
            console.print(f"[green]Duration: {duration:.1f} minutes[/green]")
            console.print(f"[green]Findings saved to: findings/[/green]")

        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            console.print(f"[red]Fatal: {e}[/red]")

    def persist_signals(self, multi_signal, top_tickers):
        """Persist the multi-signal picks to the strategy_signals table so the
        newsletter can bucket them by analyst consensus.

        Producer-owned data: one row per multi-signal ticker for today, carrying
        the strategy names that flagged it and (for the top conviction names)
        its rank. Reads stay on the read-only pool; this is the one write the
        explorer makes, through the writer pool via db.execute[_many]. A failure
        here is logged but does not fail the run -- the file findings are still
        written, and a stale strategy_picks section is better than no report.
        """
        if not multi_signal:
            logger.info("No multi-signal tickers; skipping strategy_signals persist.")
            return

        issue_date = date.today()
        conviction_rank = {t: i + 1 for i, (t, _) in enumerate(top_tickers)}
        top_set = set(conviction_rank)

        rows = []
        for ticker, signals in multi_signal.items():
            uniq = sorted(set(signals))
            rows.append((
                issue_date, ticker, uniq, len(uniq),
                ticker in top_set, conviction_rank.get(ticker),
            ))

        try:
            # Idempotent for the day: replace any earlier run's rows.
            db.execute("DELETE FROM strategy_signals WHERE issue_date = %s", (issue_date,))
            db.execute_many(
                """INSERT INTO strategy_signals
                       (issue_date, ticker, signals, signal_count,
                        is_top_conviction, conviction_rank)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (issue_date, ticker) DO UPDATE SET
                       signals           = EXCLUDED.signals,
                       signal_count      = EXCLUDED.signal_count,
                       is_top_conviction = EXCLUDED.is_top_conviction,
                       conviction_rank   = EXCLUDED.conviction_rank,
                       run_ts            = now()""",
                rows,
            )
            console.print(f"[green]✓[/green] Persisted {len(rows)} rows to strategy_signals ({issue_date})")
            logger.info(f"Persisted {len(rows)} strategy_signals rows for {issue_date}")
        except Exception as exc:  # noqa: BLE001 -- persistence is best-effort
            logger.warning(f"strategy_signals persist failed ({exc}); file findings still written.")
            console.print(f"[yellow]○[/yellow] strategy_signals persist skipped: {str(exc)[:80]}")

    def save_results(self, results, multi_signal, top_tickers, facts):
        """Save raw findings"""
        Path("findings").mkdir(exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        output = {
            'timestamp': timestamp,
            'total_strategies_run': len(results),
            'total_tickers_found': len(set(t for r in results for t in r.get('tickers_found', []))),
            'multi_signal_stocks': {t: s for t, s in multi_signal.items()},
            'top_conviction': [{'ticker': t, 'signals': s} for t, s in top_tickers],
            # Authoritative, grounded facts for the conviction picks
            'top_conviction_facts': _jsonable_rows(facts),
            'results': [{
                'strategy': r['strategy'],
                'params': r['params'],
                'tickers': r['tickers_found'][:10],
                'count': r['row_count'],
                'insight': r['interpretation'].get('key_insight', ''),
                'confidence': r['interpretation'].get('confidence', 0),
                'top_rows': _jsonable_rows(r.get('top_rows', []))
            } for r in results]
        }

        filepath = f"findings/results_{timestamp}.json"
        with open(filepath, 'w') as f:
            json.dump(output, f, indent=2, default=str)

        latest = Path("findings/latest_results.json")
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(f"results_{timestamp}.json")

        logger.info(f"Results saved to {filepath}")

    def save_report(self, report):
        """Save markdown report"""
        Path("findings").mkdir(exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        with open(f"findings/report_{timestamp}.md", 'w') as f:
            f.write("# Investment Research Report\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(report)
            f.write("\n\n---\n*Report generated by Stock Explorer Agent on MS-A1*\n")


if __name__ == "__main__":
    explorer = StockExplorer()
    explorer.run()
