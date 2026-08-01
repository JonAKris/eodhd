#!/usr/bin/env python3
"""
ssg_screener.py
===============
Scan the EODHD Postgres database, keep only the stocks that pass the
BetterInvesting / NAIC **Stock Selection Guide (SSG)** quality-growth gates,
and complete a full SSG study for each survivor.

The projections the SSG needs (future sales growth, future EPS growth, and the
future high / low P/E used to price the stock five years out) are produced with
**focus forecasting** -- Bernard T. Smith's technique of running several simple
candidate models, back-testing each of them against the most recent *known*
history, and then trusting whichever model has been most accurate lately to make
the forward projection. Nothing here replaces the investor's judgment the
handbook keeps insisting on; it just fills the fields a human would otherwise
fill by hand, and shows its work so you can override it.

The five SSG sections implemented (handbook chapters in brackets):
  1. Visual Analysis      -- historical sales / EPS growth trends           [3]
  2. Evaluate Management  -- ROE, pre-tax margin, debt-to-capital           [4]
  3. Forecast growth      -- focus-forecast future sales & EPS growth       [5]
  4. Price-Earnings hist. -- annual high/low P/E, payout, outlier removal   [6]
  5. Risk & Reward + 5-yr -- forecast prices, zones, up/down ratio, return  [7,8]

Run `python ssg_screener.py --selftest` to exercise the forecasting engine and
the SSG arithmetic on synthetic data without touching a database.

Usage
-----
    python ssg_screener.py                      # scan default US common stock
    python ssg_screener.py --exchange US --limit 500 --out ssg.csv
    python ssg_screener.py --sector "Technology" --min-market-cap 2e9
    python ssg_screener.py --buys-only          # only print/keep buy-zone names
    python ssg_screener.py --selftest           # no DB; verify the math
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Callable, Optional, Sequence

log = logging.getLogger("ssg_screener")

# ---------------------------------------------------------------------------
# Database access -- reuse the project's pool if we're running inside it,
# otherwise fall back to a self-contained psycopg connection from env vars.
# ---------------------------------------------------------------------------
try:
    from db import fetch_all_ro as _fetch_all  # type: ignore -- reads are read-only
    from db import execute as _execute, execute_many as _execute_many  # writes -> writer pool

    def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict]:
        return _fetch_all(sql, params)

    def execute(sql: str, params: tuple | dict | None = None) -> int:
        return _execute(sql, params)

    def execute_many(sql: str, params_list: list) -> int:
        return _execute_many(sql, params_list)

    _USING_PROJECT_DB = True
except Exception:  # noqa: BLE001 -- standalone mode
    _USING_PROJECT_DB = False
    import os

    def _dsn() -> str:
        # Accept either a ready-made DATABASE_URL or the project's PG_* vars.
        url = os.getenv("DATABASE_URL")
        if url:
            return url
        # Prefer the read-only role; fall back to the writer identity when
        # PG_RO_* is unset, mirroring config.ro_dsn.
        ro_user = os.getenv("PG_RO_USER", os.getenv("PG_USER", "postgres"))
        ro_password = os.getenv("PG_RO_PASSWORD", os.getenv("PG_PASSWORD", "postgres"))
        return (
            f"host={os.getenv('PG_HOST', 'localhost')} "
            f"port={os.getenv('PG_PORT', '5432')} "
            f"dbname={os.getenv('PG_DB', 'eodhd')} "
            f"user={ro_user} "
            f"password={ro_password}"
        )

    _CONN = None

    def _conn():
        global _CONN
        if _CONN is None or _CONN.closed:
            import psycopg
            from psycopg.rows import dict_row
            _CONN = psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)
        return _CONN

    def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict]:
        with _conn().cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _wdsn() -> str:
        # Writer identity for persistence. A ready-made DATABASE_URL wins;
        # otherwise use PG_USER/PG_PASSWORD (the writer role), never PG_RO_*.
        url = os.getenv("DATABASE_URL")
        if url:
            return url
        return (
            f"host={os.getenv('PG_HOST', 'localhost')} "
            f"port={os.getenv('PG_PORT', '5432')} "
            f"dbname={os.getenv('PG_DB', 'eodhd')} "
            f"user={os.getenv('PG_USER', 'postgres')} "
            f"password={os.getenv('PG_PASSWORD', 'postgres')}"
        )

    _WCONN = None

    def _wconn():
        global _WCONN
        if _WCONN is None or _WCONN.closed:
            import psycopg
            _WCONN = psycopg.connect(_wdsn(), autocommit=True)
        return _WCONN

    def execute(sql: str, params: tuple | dict | None = None) -> int:
        with _wconn().cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def execute_many(sql: str, params_list: list) -> int:
        with _wconn().cursor() as cur:
            cur.executemany(sql, params_list)
            return cur.rowcount


# ===========================================================================
# SSG thresholds -- straight from the handbook (Chapter 9 "To Buy or Not to Buy",
# Chapter 7 risk/reward). Every number here is a handbook rule of thumb, exposed
# so you can tune it to your own philosophy.
# ===========================================================================
@dataclass(frozen=True)
class SSGConfig:
    horizon_years: int = 5            # SSG always forecasts five years out
    min_history_years: int = 5        # need enough history to judge consistency
    # --- Section 1/2 quality gates ---
    min_roe: float = 0.15             # "at least 15% ROE"; great companies ~20%
    max_debt_to_cap: float = 0.33     # "debt < 33% of capitalization"
    min_sales_growth: float = 0.03    # must actually be a growth company
    min_eps_growth: float = 0.03
    max_sane_growth: float = 0.30     # reject absurd fitted growth as noise
    min_up_year_fraction: float = 0.6 # sales up in >=60% of year-over-year steps
    # --- Section 4/6 P/E rules ---
    max_high_pe: float = 30.0         # "estimated high P/Es shouldn't exceed 30"
    # --- Section 7/8 buy rules ---
    min_updown_ratio: float = 3.0     # buy only when upside/downside >= 3:1
    low_price_cushion: float = 0.20   # forecast low >= 20% below current price
    # size-based minimum acceptable total return (handbook Fig 1.3 / Ch 8)
    small_cap_ceiling: float = 1e9
    large_cap_floor: float = 1e10
    min_return_small: float = 0.15    # small caps: aim for ~15%+
    min_return_medium: float = 0.12
    min_return_large: float = 0.07    # large caps: 7-12%+


CFG = SSGConfig()


# ===========================================================================
# Focus forecasting engine
# ===========================================================================
# A "growth estimator" maps a historical level series (oldest->newest) to a
# single compound annual growth rate. Focus forecasting will back-test each of
# these and keep whichever has predicted recent history most accurately.

def _cagr(series: Sequence[float]) -> Optional[float]:
    """Compound annual growth rate from first to last positive endpoint."""
    if len(series) < 2:
        return None
    a, b = series[0], series[-1]
    n = len(series) - 1
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1.0 / n) - 1.0


def est_cagr_full(s: Sequence[float]) -> Optional[float]:
    return _cagr(s)


def est_cagr_recent3(s: Sequence[float]) -> Optional[float]:
    return _cagr(s[-4:]) if len(s) >= 4 else _cagr(s)


def est_cagr_recent5(s: Sequence[float]) -> Optional[float]:
    return _cagr(s[-6:]) if len(s) >= 6 else _cagr(s)


def est_logfit(s: Sequence[float]) -> Optional[float]:
    """Least-squares slope of log(level) vs year -- the classic SSG semi-log
    trend line. Only valid when every level is positive."""
    xs = [i for i, v in enumerate(s) if v is not None and v > 0]
    ys = [math.log(v) for v in s if v is not None and v > 0]
    if len(xs) < 3 or len(xs) != len(s):
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return math.exp(slope) - 1.0


def est_mean_yoy(s: Sequence[float]) -> Optional[float]:
    """Average of year-over-year growth rates (recency-weighted)."""
    rates, weights = [], []
    for i in range(1, len(s)):
        a, b = s[i - 1], s[i]
        if a is None or b is None or a <= 0:
            continue
        rates.append(b / a - 1.0)
        weights.append(float(i))  # newer steps weigh more
    if not rates:
        return None
    return sum(r * w for r, w in zip(rates, weights)) / sum(weights)


def est_last_yoy(s: Sequence[float]) -> Optional[float]:
    if len(s) < 2 or s[-2] is None or s[-2] <= 0 or s[-1] is None:
        return None
    return s[-1] / s[-2] - 1.0


# Ordered so ties break toward the more conservative / recent-aware method.
GROWTH_ESTIMATORS: dict[str, Callable[[Sequence[float]], Optional[float]]] = {
    "logfit_trend": est_logfit,
    "cagr_5yr": est_cagr_recent5,
    "cagr_3yr": est_cagr_recent3,
    "mean_yoy": est_mean_yoy,
    "cagr_full": est_cagr_full,
    "last_yoy": est_last_yoy,
}


@dataclass
class ForecastResult:
    method: str                       # winning estimator name
    growth: float                     # chosen forward annual growth rate
    projected_level: float            # level `horizon` years out
    backtest_error: Optional[float]   # winner's holdout sMAPE (None if no test)
    candidates: dict[str, float] = field(default_factory=dict)  # name -> growth
    analyst_growth: Optional[float] = None  # external anchor, if available


def _smape(actual: Sequence[float], pred: Sequence[float]) -> Optional[float]:
    """Symmetric MAPE -- scale-free, so it compares fairly across tickers."""
    errs = []
    for a, p in zip(actual, pred):
        if a is None or p is None:
            continue
        d = (abs(a) + abs(p))
        if d == 0:
            continue
        errs.append(abs(a - p) / (d / 2.0))
    return sum(errs) / len(errs) if errs else None


def focus_forecast(
    levels: Sequence[float],
    horizon: int = CFG.horizon_years,
    analyst_growth: Optional[float] = None,
    cap: Optional[float] = None,
) -> Optional[ForecastResult]:
    """Focus-forecast a level series.

    1. For every candidate estimator, run a rolling-origin back-test: fit on the
       data up to each origin, project the *known* remainder, and score it with
       sMAPE. The estimator with the lowest average recent error wins.
    2. Refit the winner on the full series to get the forward growth rate.
    3. If analyst guidance is supplied it is blended in as one more voice, only
       when it is not wildly out of line with the data-driven winner (the
       handbook treats analyst estimates as "additional information", not gospel).
    """
    levels = [float(v) for v in levels if v is not None]
    if len(levels) < 3:
        return None

    # ---- 1. back-test each candidate over the last min(3, n-2) origins -------
    n = len(levels)
    test_points = min(3, n - 2)
    scores: dict[str, list[float]] = {name: [] for name in GROWTH_ESTIMATORS}
    for h in range(1, test_points + 1):
        train = levels[: n - h]
        actual_tail = levels[n - h:]
        base = train[-1]
        for name, est in GROWTH_ESTIMATORS.items():
            g = est(train)
            if g is None:
                continue
            pred = [base * ((1 + g) ** k) for k in range(1, h + 1)]
            e = _smape(actual_tail, pred)
            if e is not None:
                scores[name].append(e)

    # average each candidate's error; ignore ones that never scored
    avg_err = {
        name: sum(errs) / len(errs)
        for name, errs in scores.items()
        if errs
    }

    # ---- 2. compute every candidate's forward growth on the full series ------
    full_growth: dict[str, float] = {}
    for name, est in GROWTH_ESTIMATORS.items():
        g = est(levels)
        if g is not None and math.isfinite(g):
            full_growth[name] = g
    if not full_growth:
        return None

    # ---- 3. pick the winner: lowest back-test error among those we could run -
    if avg_err:
        candidates_ranked = sorted(
            (name for name in avg_err if name in full_growth),
            key=lambda nm: avg_err[nm],
        )
    else:
        candidates_ranked = list(full_growth.keys())

    if not candidates_ranked:
        candidates_ranked = list(full_growth.keys())

    winner = candidates_ranked[0]
    g = full_growth[winner]

    # ---- optional analyst blend ---------------------------------------------
    if analyst_growth is not None and math.isfinite(analyst_growth):
        # only blend when analyst view is within 10 pts of the data-driven pick
        if abs(analyst_growth - g) <= 0.10:
            g = 0.5 * g + 0.5 * analyst_growth
            winner = f"{winner}+analyst"

    # ---- handbook sanity cap -------------------------------------------------
    if cap is not None:
        g = max(-cap, min(g, cap))

    projected = levels[-1] * ((1 + g) ** horizon)
    return ForecastResult(
        method=winner,
        growth=g,
        projected_level=projected,
        backtest_error=avg_err.get(winner.replace("+analyst", "")),
        candidates=full_growth,
        analyst_growth=analyst_growth,
    )


# ===========================================================================
# Data loaders
# ===========================================================================
def load_universe(
    exchange: Optional[str],
    sector: Optional[str],
    min_market_cap: Optional[float],
    limit: Optional[int],
) -> list[dict]:
    """Candidate tickers joined to their fundamentals header row.

    Only active common stock with a market cap on file is considered -- the
    handbook is about operating companies, not ETFs, funds or delisted shells.
    """
    where = ["s.is_active = true", "s.delisted_on IS NULL"]
    params: list = []
    # EODHD stores plain equities as type 'Common Stock'
    where.append("(s.type ILIKE %s OR s.type IS NULL)")
    params.append("common stock")
    if exchange:
        where.append("s.exchange_code = %s")
        params.append(exchange)
    if sector:
        where.append("f.sector = %s")
        params.append(sector)
    if min_market_cap:
        where.append("f.market_cap >= %s")
        params.append(min_market_cap)

    sql = f"""
        SELECT s.ticker, s.name, s.exchange_code, s.currency,
               f.sector, f.industry, f.market_cap, f.pe_ratio, f.eps,
               f.dividend_share, f.dividend_yield, f.return_on_equity,
               f.profit_margin, f.wall_street_target_price
          FROM symbols s
          JOIN fundamentals f ON f.ticker = s.ticker
         WHERE {' AND '.join(where)}
      ORDER BY f.market_cap DESC NULLS LAST
    """
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return fetch_all(sql, tuple(params))


def load_annual_fundamentals(ticker: str, years: int) -> list[dict]:
    """Annual sales, pre-tax income and net income, oldest->newest."""
    rows = fetch_all(
        """SELECT date, total_revenue, income_before_tax, net_income
             FROM income_statements
            WHERE ticker=%s AND period_type='yearly'
         ORDER BY date DESC LIMIT %s""",
        (ticker, years + 1),
    )
    rows.reverse()
    return rows


def load_annual_eps(ticker: str, years: int) -> dict[int, float]:
    """Annual EPS, summed from quarterly actuals. Only keep years with >=3
    quarters reported so a stub year doesn't masquerade as a full one."""
    rows = fetch_all(
        """SELECT date, eps_actual
             FROM earnings_history
            WHERE ticker=%s AND eps_actual IS NOT NULL
         ORDER BY date DESC LIMIT %s""",
        (ticker, years * 4 + 4),
    )
    by_year: dict[int, list[float]] = {}
    for r in rows:
        d = r["date"]
        if d is None:
            continue
        by_year.setdefault(d.year, []).append(float(r["eps_actual"]))
    return {y: sum(v) for y, v in by_year.items() if len(v) >= 3}


def load_latest_balance(ticker: str) -> Optional[dict]:
    rows = fetch_all(
        """SELECT date, long_term_debt, short_term_debt,
                  total_stockholder_equity
             FROM balance_sheets
            WHERE ticker=%s AND period_type='yearly'
         ORDER BY date DESC LIMIT 1""",
        (ticker,),
    )
    return rows[0] if rows else None


def load_price_extremes(ticker: str, years: int) -> dict[int, tuple[float, float]]:
    """Per-calendar-year (low, high) close-adjusted price extremes."""
    since = date.today() - timedelta(days=365 * (years + 1))
    rows = fetch_all(
        """SELECT date, low, high
             FROM eod_prices
            WHERE ticker=%s AND date >= %s
         ORDER BY date ASC""",
        (ticker, since),
    )
    out: dict[int, list[float]] = {}
    for r in rows:
        if r["low"] is None or r["high"] is None:
            continue
        y = r["date"].year
        lo, hi = out.setdefault(y, [float("inf"), float("-inf")])
        out[y] = [min(lo, float(r["low"])), max(hi, float(r["high"]))]
    return {y: (v[0], v[1]) for y, v in out.items() if v[0] != float("inf")}


def load_last_price(ticker: str) -> Optional[float]:
    rows = fetch_all(
        "SELECT close FROM eod_prices WHERE ticker=%s ORDER BY date DESC LIMIT 1",
        (ticker,),
    )
    return float(rows[0]["close"]) if rows and rows[0]["close"] is not None else None


def load_analyst_growth(ticker: str) -> Optional[float]:
    """Best available forward EPS growth estimate from earnings_trend."""
    rows = fetch_all(
        """SELECT period, growth
             FROM earnings_trend
            WHERE ticker=%s AND growth IS NOT NULL
              AND period IN ('+1y','0y','+5y')
         ORDER BY date DESC""",
        (ticker,),
    )
    # prefer a long-range estimate, else next-year
    for want in ("+5y", "+1y", "0y"):
        for r in rows:
            if r["period"] == want and r["growth"] is not None:
                return float(r["growth"])
    return None


# ===========================================================================
# SSG assembly
# ===========================================================================
@dataclass
class SSGResult:
    ticker: str
    name: str
    sector: Optional[str]
    market_cap: Optional[float]
    current_price: Optional[float]
    current_eps: Optional[float]
    # section 1/2
    hist_sales_growth: Optional[float] = None
    hist_eps_growth: Optional[float] = None
    roe: Optional[float] = None
    pretax_margin: Optional[float] = None
    debt_to_cap: Optional[float] = None
    # section 3 (focus forecast)
    fc_sales_method: Optional[str] = None
    fc_sales_growth: Optional[float] = None
    fc_eps_method: Optional[str] = None
    fc_eps_growth: Optional[float] = None
    fc_backtest_err: Optional[float] = None
    projected_eps_5yr: Optional[float] = None
    # section 4/6
    high_pe: Optional[float] = None
    low_pe: Optional[float] = None
    payout_ratio: Optional[float] = None
    # section 7/8
    forecast_high_price: Optional[float] = None
    forecast_low_price: Optional[float] = None
    buy_below: Optional[float] = None
    sell_above: Optional[float] = None
    updown_ratio: Optional[float] = None
    price_appreciation_cagr: Optional[float] = None
    avg_yield: Optional[float] = None
    total_return: Optional[float] = None
    # verdict
    quality_pass: bool = False
    is_buy: bool = False
    zone: Optional[str] = None
    reasons: list[str] = field(default_factory=list)


def _avg_pe_with_outlier_removal(pe_values: list[float]) -> Optional[float]:
    """Average a P/E series after dropping the single most extreme value once
    there are enough points -- the handbook's "remove P/Es that don't fit"."""
    vals = [v for v in pe_values if v is not None and v > 0 and math.isfinite(v)]
    if not vals:
        return None
    if len(vals) >= 5:
        vals_sorted = sorted(vals)
        vals = vals_sorted[:-1]  # drop the highest outlier for a conservative avg
    return sum(vals) / len(vals)


def size_min_return(market_cap: Optional[float]) -> float:
    if market_cap is None:
        return CFG.min_return_medium
    if market_cap < CFG.small_cap_ceiling:
        return CFG.min_return_small
    if market_cap >= CFG.large_cap_floor:
        return CFG.min_return_large
    return CFG.min_return_medium


def build_ssg(row: dict) -> SSGResult:
    """Run the full SSG for one candidate row from `load_universe`."""
    ticker = row["ticker"]
    res = SSGResult(
        ticker=ticker,
        name=row.get("name") or ticker,
        sector=row.get("sector"),
        market_cap=_f(row.get("market_cap")),
        current_price=None,
        current_eps=_f(row.get("eps")),
    )

    # ---- gather history ------------------------------------------------------
    inc = load_annual_fundamentals(ticker, CFG.min_history_years + 3)
    eps_by_year = load_annual_eps(ticker, CFG.min_history_years + 3)
    if len(inc) < CFG.min_history_years:
        res.reasons.append(f"insufficient annual history ({len(inc)} yrs)")
        return res

    years = [r["date"].year for r in inc]
    sales = [_f(r["total_revenue"]) for r in inc]
    pretax = [_f(r["income_before_tax"]) for r in inc]
    eps_series = [eps_by_year.get(y) for y in years]

    # align EPS: if quarterly sums are too sparse, fall back to net income proxy
    if sum(1 for e in eps_series if e is not None) < CFG.min_history_years:
        eps_series = None

    # ---- Section 1: Visual Analysis (historical growth) ----------------------
    clean_sales = [v for v in sales if v is not None]
    res.hist_sales_growth = est_logfit(clean_sales) or _cagr(clean_sales)
    if eps_series:
        clean_eps = [v for v in eps_series if v is not None]
        res.hist_eps_growth = est_logfit(clean_eps) or _cagr(clean_eps)
    res.current_price = load_last_price(ticker)

    # sales consistency: fraction of up years
    ups = sum(
        1 for a, b in zip(clean_sales, clean_sales[1:]) if b is not None and a is not None and b > a
    )
    steps = max(len(clean_sales) - 1, 1)
    up_fraction = ups / steps

    # ---- Section 2: Evaluate Management --------------------------------------
    res.roe = _f(row.get("return_on_equity"))
    bal = load_latest_balance(ticker)
    if bal:
        ltd = _f(bal.get("long_term_debt")) or 0.0
        std = _f(bal.get("short_term_debt")) or 0.0
        eq = _f(bal.get("total_stockholder_equity"))
        debt = ltd + std
        if eq and (debt + eq) > 0:
            res.debt_to_cap = debt / (debt + eq)
        # ROE fallback from statements if the header value is missing
        if res.roe is None and eq and eq > 0 and inc[-1].get("net_income"):
            res.roe = _f(inc[-1]["net_income"]) / eq
    if pretax and sales and pretax[-1] is not None and sales[-1]:
        res.pretax_margin = pretax[-1] / sales[-1]

    # ---- quality gate (handbook: no point continuing if this fails) ----------
    res.quality_pass, gate_reasons = _quality_gate(res, up_fraction)
    res.reasons.extend(gate_reasons)
    if not res.quality_pass:
        return res

    # ---- Section 3: focus-forecast future growth -----------------------------
    analyst_g = load_analyst_growth(ticker)
    sales_fc = focus_forecast(clean_sales, cap=CFG.max_sane_growth)
    if sales_fc:
        res.fc_sales_method = sales_fc.method
        res.fc_sales_growth = sales_fc.growth

    eps_levels = [v for v in (eps_series or []) if v is not None]
    eps_fc = focus_forecast(eps_levels, analyst_growth=analyst_g,
                            cap=CFG.max_sane_growth) if len(eps_levels) >= 3 else None
    if eps_fc:
        res.fc_eps_method = eps_fc.method
        res.fc_eps_growth = eps_fc.growth
        res.fc_backtest_err = eps_fc.backtest_error
        # Handbook caution: don't let EPS outgrow sales in the forecast.
        if res.fc_sales_growth is not None and res.fc_eps_growth > res.fc_sales_growth:
            res.fc_eps_growth = res.fc_sales_growth
            res.reasons.append("EPS growth capped at sales growth (handbook rule)")

    base_eps = res.current_eps if (res.current_eps and res.current_eps > 0) else (
        eps_levels[-1] if eps_levels else None)
    if base_eps and res.fc_eps_growth is not None:
        res.projected_eps_5yr = base_eps * ((1 + res.fc_eps_growth) ** CFG.horizon_years)

    # ---- Section 4/6: P/E history --------------------------------------------
    price_ext = load_price_extremes(ticker, CFG.min_history_years)
    high_pes, low_pes = [], []
    for y, (lo, hi) in price_ext.items():
        e = eps_by_year.get(y)
        if e and e > 0:
            high_pes.append(hi / e)
            low_pes.append(lo / e)
    res.high_pe = _avg_pe_with_outlier_removal(high_pes)
    res.low_pe = _avg_pe_with_outlier_removal(low_pes)
    # handbook: estimated high P/E generally shouldn't exceed 30
    if res.high_pe:
        res.high_pe = min(res.high_pe, CFG.max_high_pe)

    div_share = _f(row.get("dividend_share"))
    if div_share and base_eps and base_eps > 0:
        res.payout_ratio = div_share / base_eps

    # ---- Section 7/8: Risk & Reward + Five-Year Potential --------------------
    if (res.projected_eps_5yr and res.high_pe and res.low_pe
            and base_eps and res.current_price):
        # High price = high P/E * projected (highest) EPS
        res.forecast_high_price = res.high_pe * res.projected_eps_5yr
        # Growth-company low price = low P/E * current (low) EPS
        res.forecast_low_price = res.low_pe * base_eps

        hi, lo, cur = res.forecast_high_price, res.forecast_low_price, res.current_price
        if hi > lo:
            rng = hi - lo
            res.buy_below = lo + rng / 3.0
            res.sell_above = hi - rng / 3.0
            # zone verdict
            if cur <= res.buy_below:
                res.zone = "BUY"
            elif cur >= res.sell_above:
                res.zone = "SELL"
            else:
                res.zone = "HOLD"
            # upside / downside ratio
            downside = cur - lo
            upside = hi - cur
            if downside > 0:
                res.updown_ratio = upside / downside
            elif upside > 0:
                res.updown_ratio = float("inf")  # price at/below forecast low
            # price appreciation CAGR and total return
            if cur > 0 and hi > 0:
                res.price_appreciation_cagr = (hi / cur) ** (1 / CFG.horizon_years) - 1
            res.avg_yield = _f(row.get("dividend_yield"))
            res.total_return = (res.price_appreciation_cagr or 0) + (res.avg_yield or 0)

    # ---- buy decision --------------------------------------------------------
    res.is_buy, buy_reasons = _buy_decision(res)
    res.reasons.extend(buy_reasons)
    return res


def _quality_gate(res: SSGResult, up_fraction: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    ok = True
    if res.hist_sales_growth is None or res.hist_sales_growth < CFG.min_sales_growth:
        ok = False
        reasons.append(f"sales growth too low ({_pct(res.hist_sales_growth)})")
    if res.hist_eps_growth is None or res.hist_eps_growth < CFG.min_eps_growth:
        ok = False
        reasons.append(f"EPS growth too low/unavailable ({_pct(res.hist_eps_growth)})")
    if up_fraction < CFG.min_up_year_fraction:
        ok = False
        reasons.append(f"inconsistent sales ({up_fraction:.0%} up-years)")
    if res.roe is None or res.roe < CFG.min_roe:
        ok = False
        reasons.append(f"ROE below 15% ({_pct(res.roe)})")
    if res.debt_to_cap is not None and res.debt_to_cap > CFG.max_debt_to_cap:
        ok = False
        reasons.append(f"debt/cap above 33% ({_pct(res.debt_to_cap)})")
    if res.pretax_margin is not None and res.pretax_margin <= 0:
        ok = False
        reasons.append("negative pre-tax margin")
    return ok, reasons


def _buy_decision(res: SSGResult) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if res.zone is None or res.updown_ratio is None:
        return False, ["price zones unavailable"]
    hurdle = size_min_return(res.market_cap)
    is_buy = True
    if res.zone != "BUY":
        is_buy = False
        reasons.append(f"not in buy zone (current in {res.zone})")
    if res.updown_ratio < CFG.min_updown_ratio:
        is_buy = False
        reasons.append(f"upside/downside {res.updown_ratio:.1f}:1 < 3:1")
    if res.updown_ratio > 9:
        reasons.append("upside/downside >9:1 -- forecast low may be too high")
    # forecast low should sit ~20% below current price for a real cushion
    if (res.current_price and res.forecast_low_price
            and res.forecast_low_price > res.current_price * (1 - CFG.low_price_cushion)):
        reasons.append("forecast low <20% below price (thin downside cushion)")
    if res.total_return is not None and res.total_return < hurdle:
        is_buy = False
        reasons.append(f"total return {_pct(res.total_return)} < {hurdle:.0%} hurdle")
    if is_buy:
        reasons.append(f"BUY: {res.updown_ratio:.1f}:1 up/down, "
                       f"{_pct(res.total_return)} projected return")
    return is_buy, reasons


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(v) -> str:
    return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "n/a"


# ===========================================================================
# Runner
# ===========================================================================
CSV_FIELDS = [
    "ticker", "name", "sector", "market_cap", "current_price", "current_eps",
    "hist_sales_growth", "hist_eps_growth", "roe", "pretax_margin", "debt_to_cap",
    "fc_sales_method", "fc_sales_growth", "fc_eps_method", "fc_eps_growth",
    "fc_backtest_err", "projected_eps_5yr", "high_pe", "low_pe", "payout_ratio",
    "forecast_high_price", "forecast_low_price", "buy_below", "sell_above",
    "updown_ratio", "price_appreciation_cagr", "avg_yield", "total_return",
    "quality_pass", "is_buy", "zone",
]


def run(args: argparse.Namespace) -> int:
    universe = load_universe(
        exchange=args.exchange,
        sector=args.sector,
        min_market_cap=args.min_market_cap,
        limit=args.limit,
    )
    log.info("Universe: %d candidate tickers", len(universe))

    results: list[SSGResult] = []
    for i, row in enumerate(universe, 1):
        try:
            res = build_ssg(row)
        except Exception as e:  # noqa: BLE001 -- keep scanning on per-ticker error
            log.warning("SSG failed for %s: %s", row.get("ticker"), e)
            continue
        results.append(res)
        if i % 100 == 0:
            log.info("  processed %d/%d", i, len(universe))

    passed = [r for r in results if r.quality_pass]
    buys = [r for r in results if r.is_buy]
    log.info("Quality-growth companies: %d | current buys: %d",
             len(passed), len(buys))

    if not getattr(args, "no_persist", False):
        # Persist the full quality set (not just --buys-only) so the newsletter
        # has Buy/Hold/Sell material regardless of the CSV filter.
        persist_results(passed)

    keep = buys if args.buys_only else passed
    keep.sort(key=lambda r: (r.total_return or -1), reverse=True)

    _write_csv(args.out, keep)
    _print_summary(keep, args.buys_only)
    return 0


def _finite(x):
    """Map inf/-inf/NaN to None so they never hit a numeric column.
    The SSG up/down ratio is deliberately set to +inf when price sits at or
    below the forecast low; that is a display concept, not a storable number.
    """
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return x
    return None if not math.isfinite(xf) else x


def persist_results(rows: list["SSGResult"], issue_date: Optional[date] = None) -> None:
    """Upsert the quality-growth results into ssg_results for the newsletter.

    Producer-owned data: build_newsletter reads this table and buckets the
    names Buy/Hold/Sell. Best-effort -- a failure is logged but never fails the
    screen, so the CSV output is unaffected. Idempotent per issue date.
    """
    if not rows:
        log.info("No SSG rows to persist.")
        return
    issue_date = issue_date or date.today()

    params = []
    for r in rows:
        params.append((
            issue_date, r.ticker, r.name, r.sector,
            _finite(r.market_cap), _finite(r.current_price),
            r.zone, bool(r.is_buy), bool(r.quality_pass),
            _finite(r.buy_below), _finite(r.sell_above), _finite(r.updown_ratio),
            _finite(r.total_return), _finite(r.price_appreciation_cagr),
            _finite(r.avg_yield), _finite(r.forecast_high_price),
            _finite(r.forecast_low_price), _finite(r.projected_eps_5yr),
            _finite(r.high_pe), _finite(r.low_pe), _finite(r.roe),
            list(r.reasons or []),
        ))

    try:
        execute("DELETE FROM ssg_results WHERE issue_date = %s", (issue_date,))
        execute_many(
            """INSERT INTO ssg_results
                 (issue_date, ticker, name, sector, market_cap, current_price, zone,
                  is_buy, quality_pass, buy_below, sell_above, updown_ratio, total_return,
                  price_appreciation_cagr, avg_yield, forecast_high_price, forecast_low_price,
                  projected_eps_5yr, high_pe, low_pe, roe, reasons)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (issue_date, ticker) DO UPDATE SET
                  name=EXCLUDED.name, sector=EXCLUDED.sector, market_cap=EXCLUDED.market_cap,
                  current_price=EXCLUDED.current_price, zone=EXCLUDED.zone,
                  is_buy=EXCLUDED.is_buy, quality_pass=EXCLUDED.quality_pass,
                  buy_below=EXCLUDED.buy_below, sell_above=EXCLUDED.sell_above,
                  updown_ratio=EXCLUDED.updown_ratio, total_return=EXCLUDED.total_return,
                  price_appreciation_cagr=EXCLUDED.price_appreciation_cagr,
                  avg_yield=EXCLUDED.avg_yield, forecast_high_price=EXCLUDED.forecast_high_price,
                  forecast_low_price=EXCLUDED.forecast_low_price,
                  projected_eps_5yr=EXCLUDED.projected_eps_5yr, high_pe=EXCLUDED.high_pe,
                  low_pe=EXCLUDED.low_pe, roe=EXCLUDED.roe, reasons=EXCLUDED.reasons,
                  run_ts=now()""",
            params,
        )
        log.info("Persisted %d SSG rows to ssg_results (%s)", len(params), issue_date)
    except Exception as exc:  # noqa: BLE001 -- persistence is best-effort
        log.warning("ssg_results persist failed (%s); CSV output unaffected.", exc)


def _write_csv(path: str, rows: list[SSGResult]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: v for k, v in asdict(r).items() if k in CSV_FIELDS})
    log.info("Wrote %d rows -> %s", len(rows), path)


def _print_summary(rows: list[SSGResult], buys_only: bool) -> None:
    label = "BUY candidates" if buys_only else "quality-growth companies"
    print(f"\nTop {label} (ranked by projected total return):\n")
    header = f"{'TICKER':<12}{'ZONE':<6}{'U/D':>6}{'RET':>8}{'SALESg':>8}{'EPSg':>8}  METHOD"
    print(header)
    print("-" * len(header))
    for r in rows[:40]:
        ud = f"{r.updown_ratio:.1f}" if r.updown_ratio not in (None, float('inf')) else "inf"
        print(f"{r.ticker:<12}{(r.zone or '-'):<6}{ud:>6}"
              f"{_pct(r.total_return):>8}{_pct(r.fc_sales_growth):>8}"
              f"{_pct(r.fc_eps_growth):>8}  {r.fc_eps_method or '-'}")


# ===========================================================================
# Self-test -- no database required
# ===========================================================================
def _selftest() -> int:
    print("Running focus-forecasting + SSG self-test (no DB)...\n")
    ok = True

    # 1. clean 12%/yr compounder -> focus forecast should recover ~12%
    series = [100 * (1.12 ** k) for k in range(8)]
    fc = focus_forecast(series)
    assert fc is not None
    print(f"[1] pure 12% series -> method={fc.method}, growth={fc.growth:.4f}, "
          f"proj5={fc.projected_level:.1f}")
    ok &= abs(fc.growth - 0.12) < 0.01

    # 2. noisy but upward series -> growth positive and bounded
    noisy = [100, 108, 121, 130, 129, 150, 168, 172]
    fc2 = focus_forecast(noisy)
    print(f"[2] noisy series -> method={fc2.method}, growth={fc2.growth:.4f}, "
          f"backtest_sMAPE={fc2.backtest_error}")
    ok &= (0.0 < fc2.growth < 0.30)

    # 3. cap enforced
    steep = [10 * (1.60 ** k) for k in range(6)]
    fc3 = focus_forecast(steep, cap=0.30)
    print(f"[3] steep series with 30% cap -> growth={fc3.growth:.4f}")
    ok &= (fc3.growth <= 0.30 + 1e-9)

    # 4. analyst blend only when close
    fc4 = focus_forecast(series, analyst_growth=0.14)
    print(f"[4] 12% series + 14% analyst -> method={fc4.method}, "
          f"growth={fc4.growth:.4f} (blended)")
    ok &= ("analyst" in fc4.method and abs(fc4.growth - 0.13) < 0.005)

    # 5. full SSG arithmetic on a synthetic buy-zone stock
    res = SSGResult(ticker="TEST", name="Test Co", sector="Tech",
                    market_cap=5e9, current_price=100.0, current_eps=5.0)
    res.quality_pass = True
    res.fc_sales_growth = 0.12
    res.fc_eps_growth = 0.12
    res.projected_eps_5yr = 5.0 * (1.12 ** 5)   # ~8.81
    res.high_pe = 22.0
    res.low_pe = 14.0
    res.forecast_high_price = res.high_pe * res.projected_eps_5yr
    res.forecast_low_price = res.low_pe * res.current_eps
    hi, lo, cur = res.forecast_high_price, res.forecast_low_price, res.current_price
    rng = hi - lo
    res.buy_below = lo + rng / 3
    res.sell_above = hi - rng / 3
    res.zone = ("BUY" if cur <= res.buy_below
                else "SELL" if cur >= res.sell_above else "HOLD")
    res.updown_ratio = (hi - cur) / (cur - lo)
    res.price_appreciation_cagr = (hi / cur) ** (1 / 5) - 1
    res.avg_yield = 0.01
    res.total_return = res.price_appreciation_cagr + res.avg_yield
    is_buy, reasons = _buy_decision(res)
    print(f"[5] SSG: high={hi:.1f} low={lo:.1f} zone={res.zone} "
          f"U/D={res.updown_ratio:.2f} return={_pct(res.total_return)} "
          f"is_buy={is_buy}")
    ok &= hi > lo and res.updown_ratio > 0

    print("\nSELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ===========================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exchange", help="exchange_code filter, e.g. US")
    p.add_argument("--sector", help="fundamentals.sector filter")
    p.add_argument("--min-market-cap", type=float, default=None,
                   help="minimum market cap (e.g. 1e9)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of candidates scanned")
    p.add_argument("--out", default="ssg_results.csv", help="output CSV path")
    p.add_argument("--buys-only", action="store_true",
                   help="only keep names currently in the buy zone")
    p.add_argument("--no-persist", action="store_true",
                   help="skip writing results to the ssg_results table (CSV only)")
    p.add_argument("--selftest", action="store_true",
                   help="run the offline math self-test and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.selftest:
        return _selftest()
    if not _USING_PROJECT_DB:
        log.info("Standalone DB mode (set DATABASE_URL or PG_* env vars).")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

