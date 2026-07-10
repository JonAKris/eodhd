#!/usr/bin/env python3
"""
backtest.py — Point-in-time factor backtest harness for the `eodhd` database.

Design goals (per project discipline):
  * NO look-ahead: every fundamental is anchored at filing_date, never period-end.
  * NO survivorship leak BY DESIGN: the universe layer keys on symbols.delisted_on,
    so the day delisted tickers are backfilled it becomes point-in-time honest with
    zero code changes. Until then it returns the survivor set and STAMPS every result
    as survivorship-biased so a biased run can never be mistaken for a clean one.
  * Scoring is information-coefficient-first (is the signal REAL?) before profit framing.
  * No invented columns: validate_schema() checks the DB at startup and fails loudly
    rather than silently producing garbage if a column name differs from assumption.

Signals implemented end-to-end, all against VERIFIED columns:
  * momentum   — 12-1 price momentum            (Tier A, price-only)
  * quality    — ROE + gross margin composite   (Tier C, quarterly fundamentals)
  * value      — earnings/FCF yield + EBITDA/EV  (Tier C, annual fundamentals + mktcap)
  * piotroski  — 9-point F-score                 (Tier C, annual current-vs-prior)

Usage:
    python backtest.py --validate
    python backtest.py --signal value
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ----------------------------------------------------------------------------
# Configuration  (flagged judgment calls live here, not buried in the code)
# ----------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"postgresql+psycopg2://{os.getenv('PG_USER', 'jon')}:{os.getenv('PG_PASSWORD', '')}"
    f"@{os.getenv('PG_HOST', 'localhost')}:{os.getenv('PG_PORT', '5432')}"
    f"/{os.getenv('PG_DB', 'eodhd')}"
)

BENCHMARK = "SPY.US"

# --- Literals to confirm against your DB (run --validate first) -------------
PRICE_COL = os.getenv("PRICE_COL", "adjusted_close")
QUARTERLY_LITERAL = os.getenv("QUARTERLY_LITERAL", "quarterly")  # period_type for quarterly
ANNUAL_LITERAL = os.getenv("ANNUAL_LITERAL", "yearly")           # period_type for annual
EQUITY_TYPES = ("Common Stock",)
LIQUIDITY_PRICE_COL = "close"   # raw close for the $5 floor (NOT adjusted_close)
# ----------------------------------------------------------------------------

DEFAULT_START = date(2023, 1, 1)
DEFAULT_END = date(2026, 3, 31)
HOLDOUT_START = date(2025, 7, 1)

FILING_LAG_FALLBACK_DAYS = 45
DECILES = 10
PRICE_ASOF_TOLERANCE_DAYS = 7
PRICE_FLOOR = 5.0               # liquidity screen: min raw close ($) at rebalance
WINSORIZE_PCT = 0.01            # clip fwd returns at 1st/99th pct before decile means
DISCRETE_SIGNALS = {"piotroski"}  # scored by value bins, not quantile deciles

REQUIRED_COLUMNS = {
    "symbols": ["ticker", "type", "is_active", "delisted_on"],
    "eod_prices": ["ticker", "date", PRICE_COL, LIQUIDITY_PRICE_COL],
    "income_statements": ["ticker", "date", "period_type", "filing_date",
                          "total_revenue", "gross_profit", "net_income", "ebitda"],
    "balance_sheets": ["ticker", "date", "period_type", "filing_date",
                       "total_assets", "total_current_assets",
                       "total_current_liabilities", "long_term_debt",
                       "short_term_debt", "cash", "total_stockholder_equity"],
    "cash_flow_statements": ["ticker", "date", "period_type", "filing_date",
                             "operating_cash_flow", "free_cash_flow"],
    "shares_outstanding": ["ticker", "date", "frequency", "shares"],
}


# ----------------------------------------------------------------------------
# Results container — carries the survivorship stamp through to the output.
# ----------------------------------------------------------------------------

@dataclass
class BacktestResult:
    signal: str
    n_rebalances: int
    mean_ic: float
    ic_ir: float
    ic_hit_rate: float
    decile_monotonicity: float
    top_minus_bottom: float
    holdout_mean_ic: float
    survivorship_biased: bool
    survivorship_note: str
    per_date: pd.DataFrame = field(default=None, repr=False)

    def summary(self) -> str:
        d = {k: v for k, v in asdict(self).items() if k != "per_date"}
        return "\n".join(f"  {k:22s}: {v}" for k, v in d.items())


# ----------------------------------------------------------------------------
# Schema validation — anti-hallucination guard. Verify, never assume.
# ----------------------------------------------------------------------------

def validate_schema(engine) -> None:
    missing = []
    with engine.connect() as conn:
        for table, cols in REQUIRED_COLUMNS.items():
            present = pd.read_sql(
                text("""SELECT column_name FROM information_schema.columns
                        WHERE table_name = :t"""),
                conn, params={"t": table},
            )["column_name"].tolist()
            missing += [f"{table}.{c}" for c in cols if c not in present]

        pt = pd.read_sql(
            text("SELECT DISTINCT period_type FROM income_statements"), conn
        )["period_type"].tolist()
        typ = pd.read_sql(
            text("SELECT DISTINCT type FROM symbols WHERE type IS NOT NULL"), conn
        )["type"].tolist()

    print("Distinct income_statements.period_type values:", pt)
    print(f"  -> QUARTERLY_LITERAL={QUARTERLY_LITERAL!r}, ANNUAL_LITERAL={ANNUAL_LITERAL!r}; "
          "confirm both appear above.")
    print("Distinct symbols.type values:", sorted(typ)[:25],
          "..." if len(typ) > 25 else "")
    print(f"  -> EQUITY_TYPES={EQUITY_TYPES}; confirm those appear above.")

    if missing:
        raise SystemExit(
            "Schema validation FAILED. Missing columns:\n  " + "\n  ".join(missing)
            + "\n\nFix the config constants at the top of this file, then re-run."
        )
    print("\nSchema validation passed: all required columns present.")


# ----------------------------------------------------------------------------
# Rebalance calendar
# ----------------------------------------------------------------------------

def rebalance_dates(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.date_range(start, end, freq="ME")]


# ----------------------------------------------------------------------------
# Universe — survivorship-aware BY DESIGN via symbols.delisted_on.
# ----------------------------------------------------------------------------

def universe(engine, as_of: date) -> list[str]:
    sql = text(f"""
        WITH live AS (
            SELECT ticker FROM symbols
            WHERE type = ANY(:types)
              AND (delisted_on IS NULL OR delisted_on > :as_of)
        ),
        px AS (
            SELECT DISTINCT ON (ticker) ticker, {LIQUIDITY_PRICE_COL} AS close_px
            FROM eod_prices
            WHERE date <= :as_of AND date >= :lo
              AND {LIQUIDITY_PRICE_COL} IS NOT NULL
            ORDER BY ticker, date DESC
        )
        SELECT l.ticker
        FROM live l JOIN px p USING (ticker)
        WHERE p.close_px >= :floor
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={
            "types": list(EQUITY_TYPES),
            "as_of": as_of,
            "lo": as_of - timedelta(days=PRICE_ASOF_TOLERANCE_DAYS),
            "floor": PRICE_FLOOR,
        })
    return df["ticker"].tolist()


# ----------------------------------------------------------------------------
# Price + market-cap access
# ----------------------------------------------------------------------------

def prices_asof(engine, target: date, lookback_days: int = 10) -> pd.Series:
    sql = text(f"""
        SELECT DISTINCT ON (ticker) ticker, {PRICE_COL} AS px
        FROM eod_prices
        WHERE date <= :target AND date >= :lo AND {PRICE_COL} IS NOT NULL
        ORDER BY ticker, date DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={
            "target": target, "lo": target - timedelta(days=lookback_days)})
    return df.set_index("ticker")["px"]


def shares_asof(engine, as_of: date) -> pd.Series:
    """Most recent shares-outstanding figure per ticker on or before as_of.
    NB: shares_outstanding has NO filing_date; `date` is the as-of/report date.
    Share counts are disclosed with little lag, so date<=as_of is used directly;
    units (absolute vs millions) are irrelevant since value signals are rank-based."""
    sql = text("""
        SELECT DISTINCT ON (ticker) ticker, shares
        FROM shares_outstanding
        WHERE date <= :as_of AND shares IS NOT NULL AND shares > 0
        ORDER BY ticker, date DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"as_of": as_of})
    return df.set_index("ticker")["shares"]


def market_cap_asof(engine, as_of: date) -> pd.Series:
    px = prices_asof(engine, as_of)
    sh = shares_asof(engine, as_of)
    common = px.index.intersection(sh.index)
    return (px.loc[common] * sh.loc[common]).rename("market_cap")


def benchmark_return(engine, start: date, end: date) -> float:
    p0 = prices_asof(engine, start).get(BENCHMARK, np.nan)
    p1 = prices_asof(engine, end).get(BENCHMARK, np.nan)
    if np.isnan(p0) or np.isnan(p1) or p0 == 0:
        return np.nan
    return p1 / p0 - 1.0


# ----------------------------------------------------------------------------
# Point-in-time fundamentals — anchored at filing_date, NEVER period-end.
# ----------------------------------------------------------------------------

def _latest_statement(conn, table: str, cols: list[str], period_literal: str,
                      as_of: date, rank: int = 1) -> pd.DataFrame:
    """The rank-th most recent statement per ticker whose filing was knowable by as_of."""
    collist = ", ".join(cols)
    sql = text(f"""
        SELECT * FROM (
            SELECT ticker, {collist},
                   row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM {table}
            WHERE period_type = :pt
              AND COALESCE(filing_date, date + (:lag || ' days')::interval) <= :as_of
        ) s WHERE rn = :rank
    """)
    df = pd.read_sql(sql, conn, params={
        "pt": period_literal, "lag": FILING_LAG_FALLBACK_DAYS,
        "as_of": as_of, "rank": rank})
    return df.drop(columns=["rn"])


def quarterly_fundamentals(engine, as_of: date) -> pd.DataFrame:
    with engine.connect() as conn:
        inc = _latest_statement(conn, "income_statements",
                                ["total_revenue", "gross_profit", "net_income"],
                                QUARTERLY_LITERAL, as_of)
        bal = _latest_statement(conn, "balance_sheets",
                                ["total_stockholder_equity"], QUARTERLY_LITERAL, as_of)
    return inc.merge(bal, on="ticker")


def annual_fundamentals(engine, as_of: date, rank: int = 1) -> pd.DataFrame:
    """Merged income+balance+cashflow for the rank-th most recent ANNUAL period."""
    inc_cols = ["total_revenue", "gross_profit", "net_income", "ebitda"]
    bal_cols = ["total_assets", "total_current_assets", "total_current_liabilities",
                "long_term_debt", "short_term_debt", "cash", "total_stockholder_equity"]
    cf_cols = ["operating_cash_flow", "free_cash_flow"]
    with engine.connect() as conn:
        inc = _latest_statement(conn, "income_statements", inc_cols, ANNUAL_LITERAL, as_of, rank)
        bal = _latest_statement(conn, "balance_sheets", bal_cols, ANNUAL_LITERAL, as_of, rank)
        cf = _latest_statement(conn, "cash_flow_statements", cf_cols, ANNUAL_LITERAL, as_of, rank)
    return inc.merge(bal, on="ticker").merge(cf, on="ticker")


# ----------------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------------

def _z(s: pd.Series) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan)
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd else s * np.nan


def signal_momentum(engine, as_of: date, tickers: list[str]) -> pd.Series:
    p_start = prices_asof(engine, as_of - timedelta(days=365))
    p_skip = prices_asof(engine, as_of - timedelta(days=30))
    common = p_start.index.intersection(p_skip.index).intersection(tickers)
    mom = (p_skip.loc[common] / p_start.loc[common]) - 1.0
    return mom.replace([np.inf, -np.inf], np.nan).dropna()


def signal_quality(engine, as_of: date, tickers: list[str]) -> pd.Series:
    f = quarterly_fundamentals(engine, as_of)
    f = f[f["ticker"].isin(tickers)].set_index("ticker")
    roe = f["net_income"] / f["total_stockholder_equity"].replace(0, np.nan)
    gm = f["gross_profit"] / f["total_revenue"].replace(0, np.nan)
    return _z(roe).add(_z(gm), fill_value=np.nan).dropna()


def signal_value(engine, as_of: date, tickers: list[str]) -> pd.Series:
    """Cheapness composite (higher = cheaper): earnings yield + FCF yield + EBITDA/EV."""
    f = annual_fundamentals(engine, as_of)
    f = f[f["ticker"].isin(tickers)].set_index("ticker")
    mc = market_cap_asof(engine, as_of)
    f = f.join(mc, how="inner")
    mc_pos = f["market_cap"].replace(0, np.nan)

    earnings_yield = f["net_income"] / mc_pos
    fcf_yield = f["free_cash_flow"] / mc_pos
    ev = f["market_cap"] + f["long_term_debt"].fillna(0) \
        + f["short_term_debt"].fillna(0) - f["cash"].fillna(0)
    ebitda_to_ev = f["ebitda"] / ev.replace(0, np.nan)

    composite = _z(earnings_yield)
    composite = composite.add(_z(fcf_yield), fill_value=np.nan)
    composite = composite.add(_z(ebitda_to_ev), fill_value=np.nan)
    return composite.dropna()


def signal_piotroski(engine, as_of: date, tickers: list[str]) -> pd.Series:
    """Piotroski F-score (0-9) from current-vs-prior ANNUAL statements, filing-date gated."""
    cur = annual_fundamentals(engine, as_of, rank=1).set_index("ticker")
    prv = annual_fundamentals(engine, as_of, rank=2).set_index("ticker")
    sh_now = shares_asof(engine, as_of)
    sh_prior = shares_asof(engine, as_of - timedelta(days=365))

    common = cur.index.intersection(prv.index).intersection(tickers)
    cur, prv = cur.loc[common], prv.loc[common]

    def ratio(df, a, b):
        return df[a] / df[b].replace(0, np.nan)

    roa_c = ratio(cur, "net_income", "total_assets")
    roa_p = ratio(prv, "net_income", "total_assets")
    curr_ratio_c = ratio(cur, "total_current_assets", "total_current_liabilities")
    curr_ratio_p = ratio(prv, "total_current_assets", "total_current_liabilities")
    lev_c = ratio(cur, "long_term_debt", "total_assets")
    lev_p = ratio(prv, "long_term_debt", "total_assets")
    gm_c = ratio(cur, "gross_profit", "total_revenue")
    gm_p = ratio(prv, "gross_profit", "total_revenue")
    turn_c = ratio(cur, "total_revenue", "total_assets")
    turn_p = ratio(prv, "total_revenue", "total_assets")

    score = pd.DataFrame(index=common)
    score["roa_pos"] = (roa_c > 0).astype(int)                              # 1
    score["cfo_pos"] = (cur["operating_cash_flow"] > 0).astype(int)         # 2
    score["d_roa"] = (roa_c > roa_p).astype(int)                            # 3
    score["accruals"] = (cur["operating_cash_flow"] > cur["net_income"]).astype(int)  # 4
    score["d_lev"] = (lev_c < lev_p).astype(int)                            # 5
    score["d_curr"] = (curr_ratio_c > curr_ratio_p).astype(int)            # 6
    shares_ok = (sh_now.reindex(common) <= sh_prior.reindex(common))
    score["shares"] = shares_ok.fillna(False).astype(int)                  # 7
    score["d_gm"] = (gm_c > gm_p).astype(int)                               # 8
    score["d_turn"] = (turn_c > turn_p).astype(int)                        # 9

    return score.sum(axis=1).rename("f_score").dropna()


def signal_value_piotroski(engine, as_of: date, tickers: list[str]) -> pd.Series:
    """
    Canonical value + quality composite (Piotroski 2000): cheap AND fundamentally
    improving. Equal-weight z-scores of the value composite and the F-score.

    Equal weighting is deliberate: optimizing the value/F-score weights on this
    sample would leak the holdout. If the two are genuinely complementary, an
    unweighted sum already shows it; tuning weights is a later, separate decision.

    (Re-runs signal_value and signal_piotroski, so it re-queries fundamentals per
    rebalance — acceptable redundancy for a research harness; shareable later.)
    """
    val = signal_value(engine, as_of, tickers)        # z-summed value composite
    fsc = signal_piotroski(engine, as_of, tickers)    # 0-9 integer F-score
    common = val.index.intersection(fsc.index)
    if len(common) == 0:
        return pd.Series(dtype=float)
    combined = _z(val.loc[common]).add(
        _z(fsc.loc[common].astype(float)), fill_value=np.nan)
    return combined.dropna()


SIGNALS = {
    "momentum": signal_momentum,
    "quality": signal_quality,
    "value": signal_value,
    "piotroski": signal_piotroski,
    "value_piotroski": signal_value_piotroski,
}


# ----------------------------------------------------------------------------
# Forward returns (benchmark-relative, anchored at the rebalance date)
# ----------------------------------------------------------------------------

def forward_returns(engine, as_of: date, nxt: date, tickers: list[str]) -> pd.Series:
    p0 = prices_asof(engine, as_of)
    p1 = prices_asof(engine, nxt)
    common = p0.index.intersection(p1.index).intersection(tickers)
    raw = (p1.loc[common] / p0.loc[common]) - 1.0
    bench = benchmark_return(engine, as_of, nxt)
    rel = raw - (bench if not np.isnan(bench) else 0.0)
    return rel.replace([np.inf, -np.inf], np.nan).dropna()


# ----------------------------------------------------------------------------
# Evaluation — information coefficient first.
# ----------------------------------------------------------------------------

def _winsorize(s: pd.Series, pct: float = WINSORIZE_PCT) -> pd.Series:
    lo, hi = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lo, hi)


def evaluate_cross_section(signal: pd.Series, fwd: pd.Series, discrete: bool = False):
    df = pd.concat([signal.rename("sig"), fwd.rename("fwd")], axis=1).dropna()
    if len(df) < DECILES * 3:
        return np.nan, None
    df["fwd"] = _winsorize(df["fwd"])                 # tame microcap outliers
    ic = df["sig"].rank().corr(df["fwd"].rank())      # IC on winsorized fwd
    if discrete:
        # Discrete score (e.g. Piotroski 0-9): bin by VALUE, never qcut.
        bin_means = df.groupby(df["sig"].round().astype(int))["fwd"].mean()
    else:
        df["bin"] = pd.qcut(df["sig"].rank(method="first"), DECILES, labels=False)
        bin_means = df.groupby("bin")["fwd"].mean()
    return ic, bin_means


def run_backtest(engine, signal_name: str,
                 start: date = DEFAULT_START, end: date = DEFAULT_END) -> BacktestResult:
    fn = SIGNALS[signal_name]
    discrete = signal_name in DISCRETE_SIGNALS
    dates = rebalance_dates(start, end)
    rows = []
    decile_accum = defaultdict(list)

    for as_of, nxt in zip(dates[:-1], dates[1:]):
        tickers = universe(engine, as_of)
        if not tickers:
            continue
        sig = fn(engine, as_of, tickers)
        if sig.empty:
            continue
        fwd = forward_returns(engine, as_of, nxt, list(sig.index))
        ic, bin_means = evaluate_cross_section(sig, fwd, discrete=discrete)
        if np.isnan(ic):
            continue
        rows.append({"as_of": as_of, "n": int(len(sig)), "ic": float(ic)})
        if bin_means is not None:
            for label, v in bin_means.items():
                decile_accum[int(label)].append(v)

    per_date = pd.DataFrame(rows)
    if per_date.empty:
        raise SystemExit(
            f"No usable rebalances for {signal_name!r}. "
            "Check literals via --validate.")

    ics = per_date["ic"]
    decile_avg = pd.Series({k: np.mean(v) for k, v in decile_accum.items() if v}).sort_index()
    monotonicity = (pd.Series(decile_avg.index).rank()
                    .corr(pd.Series(decile_avg.values).rank())
                    if len(decile_avg) >= 3 else np.nan)
    tmb = (decile_avg.iloc[-1] - decile_avg.iloc[0]) if len(decile_avg) >= 2 else np.nan
    holdout = per_date[per_date["as_of"] >= HOLDOUT_START]["ic"]

    return BacktestResult(
        signal=signal_name,
        n_rebalances=len(per_date),
        mean_ic=round(float(ics.mean()), 4),
        ic_ir=round(float(ics.mean() / ics.std(ddof=0)), 4) if ics.std(ddof=0) else float("nan"),
        ic_hit_rate=round(float((ics > 0).mean()), 4),
        decile_monotonicity=round(float(monotonicity), 4) if not np.isnan(monotonicity) else float("nan"),
        top_minus_bottom=round(float(tmb), 4) if not np.isnan(tmb) else float("nan"),
        holdout_mean_ic=round(float(holdout.mean()), 4) if len(holdout) else float("nan"),
        survivorship_biased=True,
        survivorship_note=("Universe is survivor-only until delisted tickers are "
                           "backfilled (symbols.delisted_on populated). Treat returns "
                           "as an optimistic ceiling, not an estimate."),
        per_date=per_date,
    )


# ----------------------------------------------------------------------------
# Diagnostic: does Piotroski's edge concentrate AMONG cheap stocks?
# Tests the interaction Piotroski (2000) actually claimed — an additive sum can't.
# ----------------------------------------------------------------------------

def diagnostic_interaction(engine, start: date = DEFAULT_START,
                           end: date = DEFAULT_END) -> None:
    dates = rebalance_dates(start, end)
    rows = []
    for as_of, nxt in zip(dates[:-1], dates[1:]):
        tickers = universe(engine, as_of)
        if not tickers:
            continue
        val = signal_value(engine, as_of, tickers)            # higher = cheaper
        fsc = signal_piotroski(engine, as_of, tickers).astype(float)
        common = val.index.intersection(fsc.index)
        if len(common) < 40:
            continue
        fwd = forward_returns(engine, as_of, nxt, list(common))
        common = common.intersection(fwd.index)
        if len(common) < 40:
            continue
        v, f = val.loc[common], fsc.loc[common]
        r = _winsorize(fwd.loc[common])
        med = v.median()
        cheap, exp = v.index[v >= med], v.index[v < med]

        def ic(idx):
            if len(idx) < 20:
                return np.nan
            return f.loc[idx].rank().corr(r.loc[idx].rank())

        rows.append({"as_of": as_of, "cheap_ic": ic(cheap), "exp_ic": ic(exp),
                     "n_cheap": len(cheap), "n_exp": len(exp)})

    df = pd.DataFrame(rows).dropna(subset=["cheap_ic", "exp_ic"])
    if df.empty:
        raise SystemExit("Interaction diagnostic: no usable rebalances.")

    hold = df[df["as_of"] >= HOLDOUT_START]

    def fmt(s):
        return f"mean={s.mean():+.4f}  IR={s.mean()/s.std(ddof=0):+.3f}" if s.std(ddof=0) else f"mean={s.mean():+.4f}"

    print("\n=== Interaction diagnostic: Piotroski F-score IC within value halves ===")
    print(f"  rebalances used      : {len(df)}  (holdout: {len(hold)})")
    print(f"  CHEAP half  (full)   : {fmt(df['cheap_ic'])}")
    print(f"  EXPENSIVE half (full): {fmt(df['exp_ic'])}")
    print(f"  spread (cheap-exp)   : {df['cheap_ic'].mean() - df['exp_ic'].mean():+.4f}")
    if len(hold):
        print(f"  CHEAP half  (holdout): {fmt(hold['cheap_ic'])}")
        print(f"  EXPENSIVE   (holdout): {fmt(hold['exp_ic'])}")
    print(f"  avg names/half       : cheap {df['n_cheap'].mean():.0f}, "
          f"exp {df['n_exp'].mean():.0f}")
    print("\n  Read: if CHEAP IC clearly exceeds EXPENSIVE IC, the F-score's edge is")
    print("  concentrated in value names -> a CONDITIONAL strategy is justified.")
    print("  If they're similar, F-score works everywhere and additive blending is fine.")
    print("\n  NOTE: survivorship-biased universe; treat as an optimistic ceiling.")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Point-in-time factor backtest harness.")
    ap.add_argument("--validate", action="store_true",
                    help="Inspect schema and distinct enum values, then exit.")
    ap.add_argument("--signal", choices=list(SIGNALS), help="Signal to backtest.")
    ap.add_argument("--diagnostic", choices=["interaction"],
                    help="Run a structural diagnostic instead of a single-signal backtest.")
    args = ap.parse_args()

    engine = create_engine(DATABASE_URL)
    validate_schema(engine)

    if args.diagnostic == "interaction":
        diagnostic_interaction(engine)
        return
    if not args.signal:
        return

    res = run_backtest(engine, args.signal)
    print(f"\n=== Backtest: {args.signal} ===")
    print(res.summary())
    if res.survivorship_biased:
        print(f"\n  WARNING SURVIVORSHIP-BIASED RESULT - {res.survivorship_note}")


if __name__ == "__main__":
    main()