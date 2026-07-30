"""Schema-aware investment strategies - tuned for actual data formats"""
import random
import re
from typing import Dict, List

STRATEGIES = {
    "value_quality_composite": {
        "query": """
        SELECT * FROM (
            WITH ranked AS (
              SELECT f.ticker, f.name, f.sector, f.industry, f.market_cap,
                f.pe_ratio, f.peg_ratio, f.profit_margin, f.operating_margin,
                f.return_on_equity, f.quarterly_revenue_growth, f.quarterly_earnings_growth,
                f.dividend_yield, f.ebitda,
                bs.long_term_debt / NULLIF(bs.total_stockholder_equity,0) as debt_to_equity,
                bs.total_current_assets / NULLIF(bs.total_current_liabilities,0) as current_ratio,
                cf.free_cash_flow, cf.operating_cash_flow,
                NTILE(100) OVER (ORDER BY f.pe_ratio ASC) as pe_rank,
                NTILE(100) OVER (ORDER BY f.return_on_equity DESC) as roe_rank,
                NTILE(100) OVER (ORDER BY f.profit_margin DESC) as margin_rank,
                NTILE(100) OVER (ORDER BY cf.free_cash_flow DESC NULLS LAST) as fcf_rank
              FROM fundamentals f
              JOIN LATERAL (SELECT * FROM balance_sheets WHERE ticker=f.ticker AND period_type='yearly' ORDER BY date DESC LIMIT 1) bs ON true
              JOIN LATERAL (SELECT * FROM cash_flow_statements WHERE ticker=f.ticker AND period_type='yearly' ORDER BY date DESC LIMIT 1) cf ON true
              WHERE f.is_delisted=false AND f.market_cap>{min_mcap} AND f.pe_ratio>0
            )
            SELECT *, (pe_rank+roe_rank+margin_rank+fcf_rank)/4.0 as composite_score
            FROM ranked
        ) sub
        WHERE composite_score<={pctile}
        ORDER BY composite_score ASC LIMIT {limit}
        """,
        "params": {"min_mcap": [100000000, 500000000, 1000000000], "pctile": [25, 30, 40, 50], "limit": [25, 50]}
    },
    "congressional_trading": {
        "query": """
        WITH recent_trades AS (
          SELECT ticker, owner_name, relationship, transaction_code,
            COUNT(*) as trade_count,
            MAX(transaction_date) as last_date
          FROM insider_transactions
          WHERE transaction_date >= CURRENT_DATE - INTERVAL '{days} days'
          GROUP BY ticker, owner_name, relationship, transaction_code
        ),
        ticker_summary AS (
          SELECT ticker,
            COUNT(DISTINCT CASE WHEN transaction_code='P' THEN owner_name END) as buyers,
            COUNT(DISTINCT CASE WHEN transaction_code='S' THEN owner_name END) as sellers,
            SUM(CASE WHEN transaction_code='P' THEN trade_count ELSE 0 END) as buy_trades,
            SUM(CASE WHEN transaction_code='S' THEN trade_count ELSE 0 END) as sell_trades,
            STRING_AGG(DISTINCT CASE WHEN transaction_code='P' THEN owner_name END, ', ') as buyer_names,
            MAX(last_date) as latest_activity
          FROM recent_trades
          GROUP BY ticker
        )
        SELECT t.*, f.name, f.sector, f.market_cap, f.pe_ratio,
          e.close as price,
          CASE WHEN t.buyers > t.sellers THEN 'BUY_SIGNAL'
               WHEN t.sellers > t.buyers THEN 'SELL_SIGNAL'
               ELSE 'MIXED' END as signal,
          t.buy_trades - t.sell_trades as net_trades
        FROM ticker_summary t
        JOIN fundamentals f ON t.ticker=f.ticker
        JOIN eod_prices e ON t.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=t.ticker)
        WHERE f.is_delisted=false AND (t.buyers>0 OR t.sellers>0)
        ORDER BY (t.buy_trades - t.sell_trades) DESC LIMIT {limit}
        """,
        "params": {"days": [30, 60, 90, 180], "limit": [25, 50]}
    },
    "congressional_selling_alert": {
        "query": """
        WITH recent_trades AS (
          SELECT ticker, owner_name, relationship, transaction_code,
            COUNT(*) as trade_count,
            MAX(transaction_date) as last_date
          FROM insider_transactions
          WHERE transaction_date >= CURRENT_DATE - INTERVAL '{days} days'
          GROUP BY ticker, owner_name, relationship, transaction_code
        ),
        ticker_summary AS (
          SELECT ticker,
            COUNT(DISTINCT CASE WHEN transaction_code='P' THEN owner_name END) as buyers,
            COUNT(DISTINCT CASE WHEN transaction_code='S' THEN owner_name END) as sellers,
            SUM(CASE WHEN transaction_code='S' THEN trade_count ELSE 0 END) as sell_trades,
            STRING_AGG(DISTINCT CASE WHEN transaction_code='S' THEN owner_name END, ', ') as seller_names
          FROM recent_trades
          GROUP BY ticker
        )
        SELECT t.*, f.name, f.sector, f.market_cap, f.pe_ratio,
          e.close as price
        FROM ticker_summary t
        JOIN fundamentals f ON t.ticker=f.ticker
        JOIN eod_prices e ON t.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=t.ticker)
        WHERE f.is_delisted=false AND t.sellers > t.buyers
        ORDER BY t.sell_trades DESC LIMIT {limit}
        """,
        "params": {"days": [30, 60, 90, 180], "limit": [25, 50]}
    },
    "sentiment_divergence": {
        "query": """
        WITH sent AS (
          SELECT ticker,
            AVG(normalized) FILTER(WHERE date>=CURRENT_DATE-INTERVAL'{recent}d') as recent_sent,
            AVG(normalized) FILTER(WHERE date>=CURRENT_DATE-INTERVAL'{total}d' AND date<CURRENT_DATE-INTERVAL'{recent}d') as prior_sent,
            COUNT(*) FILTER(WHERE date>=CURRENT_DATE-INTERVAL'{total}d') as article_count
          FROM sentiment_daily WHERE date>=CURRENT_DATE-INTERVAL'{total}d'
          GROUP BY ticker HAVING COUNT(*)>=5
        )
        SELECT s.*, f.name, f.sector, f.market_cap,
          (e.close / NULLIF(e30.close, 0) - 1) * 100 as price_chg_30d
        FROM sent s
        JOIN fundamentals f ON s.ticker=f.ticker
        JOIN eod_prices e ON s.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=s.ticker)
        LEFT JOIN eod_prices e30 ON s.ticker=e30.ticker
          AND e30.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=s.ticker AND date <= CURRENT_DATE - 30)
        WHERE s.recent_sent>s.prior_sent AND f.is_delisted=false
        ORDER BY (s.recent_sent-s.prior_sent) DESC LIMIT {limit}
        """,
        "params": {"recent": [7, 14], "total": [30, 60], "limit": [25, 50]}
    },
    "earnings_beaters": {
        "query": """
        WITH beats AS (
          SELECT ticker, report_date, surprise_pct,
            ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY report_date DESC) as recency
          FROM earnings_history
          WHERE report_date>=CURRENT_DATE-INTERVAL'{months} months' AND surprise_pct>0
        ),
        consistent AS (
          SELECT ticker, AVG(surprise_pct) as avg_beat, COUNT(*) as total_beats
          FROM beats WHERE recency<={quarters}
          GROUP BY ticker HAVING COUNT(*)={quarters}
        )
        SELECT c.*, f.name, f.sector, f.market_cap, f.pe_ratio, f.peg_ratio,
          f.quarterly_revenue_growth, a.target_price, e.close as price,
          (a.target_price/NULLIF(e.close,0)-1)*100 as upside
        FROM consistent c
        JOIN fundamentals f ON c.ticker=f.ticker
        LEFT JOIN LATERAL (SELECT target_price FROM analyst_ratings_history WHERE ticker=c.ticker ORDER BY date DESC LIMIT 1) a ON true
        JOIN eod_prices e ON c.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=c.ticker)
        WHERE f.is_delisted=false
        ORDER BY c.avg_beat DESC LIMIT {limit}
        """,
        "params": {"months": [12, 24], "quarters": [3, 4], "limit": [25, 50]}
    },
    "earnings_miss_opportunity": {
        "query": """
        WITH misses AS (
          SELECT ticker, report_date, surprise_pct,
            ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY report_date DESC) as recency
          FROM earnings_history
          WHERE report_date>=CURRENT_DATE-INTERVAL'12 months' AND surprise_pct<0
        ),
        recent_miss AS (
          SELECT ticker, AVG(surprise_pct) as avg_miss
          FROM misses WHERE recency=1
          GROUP BY ticker
        )
        SELECT m.*, f.name, f.sector, f.market_cap, f.pe_ratio,
          f.quarterly_revenue_growth, f.quarterly_earnings_growth,
          e.close as price,
          (e.close / NULLIF(e90.close, 0) - 1) * 100 as price_chg_90d
        FROM recent_miss m
        JOIN fundamentals f ON m.ticker=f.ticker
        JOIN eod_prices e ON m.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=m.ticker)
        LEFT JOIN eod_prices e90 ON m.ticker=e90.ticker
          AND e90.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=m.ticker AND date <= CURRENT_DATE - 90)
        WHERE f.is_delisted=false AND f.pe_ratio>0
        ORDER BY m.avg_miss ASC LIMIT {limit}
        """,
        "params": {"limit": [25, 50]}
    },
    "fund_holder_conviction": {
        "query": """
        WITH funds AS (
          SELECT ticker, holder_name, MAX(report_date) as last_report,
            MAX(pct_shares) as pct, MAX(shares_held) as shares
          FROM fund_holders
          WHERE report_date>=CURRENT_DATE-INTERVAL'{months} months'
          GROUP BY ticker, holder_name
        )
        SELECT ticker, COUNT(*) as fund_count, SUM(pct) as total_pct,
          AVG(pct) as avg_pct, SUM(shares) as total_shares
        FROM funds
        GROUP BY ticker HAVING COUNT(*)>{min_funds}
        ORDER BY total_pct DESC LIMIT {limit}
        """,
        "params": {"months": [6, 12], "min_funds": [5, 10, 25], "limit": [25, 50]}
    },
    "price_momentum_leaders": {
        "query": """
        WITH returns AS (
          SELECT ticker,
            close / NULLIF(LAG(close, 21) OVER (PARTITION BY ticker ORDER BY date), 0) - 1 as ret_1m,
            close / NULLIF(LAG(close, 63) OVER (PARTITION BY ticker ORDER BY date), 0) - 1 as ret_3m,
            close / NULLIF(LAG(close, 126) OVER (PARTITION BY ticker ORDER BY date), 0) - 1 as ret_6m,
            ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) as rn
          FROM eod_prices
          WHERE date >= CURRENT_DATE - INTERVAL '12 months'
        ),
        latest AS (
          SELECT * FROM returns WHERE rn=1 AND ret_1m IS NOT NULL
        )
        SELECT l.*, f.name, f.sector, f.market_cap, f.pe_ratio,
          e.close as price
        FROM latest l
        JOIN fundamentals f ON l.ticker=f.ticker
        JOIN eod_prices e ON l.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=l.ticker)
        WHERE f.is_delisted=false AND l.ret_1m>0 AND l.ret_3m>0
        ORDER BY l.ret_6m DESC LIMIT {limit}
        """,
        "params": {"limit": [25, 50]}
    },
    "dividend_income": {
        "query": """
        WITH div_history AS (
          SELECT ticker,
            MAX(ex_date) as last_div_date,
            SUM(value) FILTER (WHERE ex_date >= CURRENT_DATE - INTERVAL '12 months') as annual_dividend,
            COUNT(*) as payments_12m
          FROM dividends
          GROUP BY ticker
        )
        SELECT d.*, f.name, f.sector, f.market_cap,
          f.dividend_yield, f.pe_ratio,
          e.close as price,
          (d.annual_dividend / NULLIF(e.close, 0)) * 100 as calculated_yield
        FROM div_history d
        JOIN fundamentals f ON d.ticker=f.ticker
        JOIN eod_prices e ON d.ticker=e.ticker AND e.date=(SELECT MAX(date) FROM eod_prices WHERE ticker=d.ticker)
        WHERE f.is_delisted=false AND d.annual_dividend>0 AND f.dividend_yield>0
        ORDER BY f.dividend_yield DESC LIMIT {limit}
        """,
        "params": {"limit": [25, 50]}
    },
    

    # ---- 1. Holder-centric: rank institutions by benchmark-relative alpha ----
    "institutional_holder_alpha": {
        "query": """
        WITH snapshot AS (
          SELECT DISTINCT ON (h.holder_name, h.ticker)
            h.holder_name, h.ticker, h.report_date, h.shares_held, h.pct_shares
          FROM institutional_holders h
          WHERE h.report_date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
            AND h.shares_held > 0
          ORDER BY h.holder_name, h.ticker, h.report_date DESC
        ),
        priced AS (
          SELECT s.*, p0.close AS price_at_report, pn.close AS price_now, pn.date AS asof_date
          FROM snapshot s
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker=s.ticker AND date<=s.report_date
                        ORDER BY date DESC LIMIT 1) p0 ON true
          JOIN LATERAL (SELECT close, date FROM eod_prices
                        WHERE ticker=s.ticker
                        -- for a fixed-horizon variant instead, add:
                        -- AND date <= s.report_date + INTERVAL '{lookback_days} days'
                        ORDER BY date DESC LIMIT 1) pn ON true
          WHERE p0.close > 0
        ),
        benched AS (
          SELECT pr.*, b0.close AS bench_at_report, bn.close AS bench_now
          FROM priced pr
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker='{benchmark}' AND date<=pr.report_date
                        ORDER BY date DESC LIMIT 1) b0 ON true
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker='{benchmark}' AND date<=pr.asof_date
                        ORDER BY date DESC LIMIT 1) bn ON true
          WHERE b0.close > 0
        ),
        positions AS (
          SELECT *,
            shares_held * price_at_report AS value_at_report,
            (price_now / price_at_report - 1.0)
              - (bench_now / bench_at_report - 1.0) AS fwd_alpha
          FROM benched
        )
        SELECT
          holder_name,
          COUNT(*)                                                           AS positions_tracked,
          ROUND(SUM(value_at_report))                                        AS book_value_at_report,
          -- value-weighted alpha vs benchmark since filing (primary rank key)
          ROUND((SUM(fwd_alpha * value_at_report)
                 / NULLIF(SUM(value_at_report), 0) * 100)::numeric, 2)        AS vw_fwd_alpha_pct,
          ROUND((AVG(fwd_alpha) * 100)::numeric, 2)                          AS avg_fwd_alpha_pct,
          -- fraction of positions that beat the benchmark
          ROUND(AVG(CASE WHEN fwd_alpha > 0 THEN 1.0 ELSE 0.0 END), 3)       AS beat_rate,
          MAX(report_date)                                                   AS latest_report
        FROM positions
        GROUP BY holder_name
        HAVING COUNT(*) >= {min_positions}
        ORDER BY vw_fwd_alpha_pct DESC
        LIMIT {limit}
        """,
        "params": {
            "lookback_days": [180, 365],
            "min_positions": [5, 10, 20],
            "benchmark": ["SPY.US"],
            "limit": [25, 50],
        },
    },
 
    # ---- 2. Ticker-centric: stocks held by top-ALPHA institutions ----
    # Top-quartile selection now uses alpha, which removes the small-cap-beta
    # tilt that raw forward return introduced into the surfaced tickers.
    "smart_institution_holdings": {
        "query": """
        WITH snapshot AS (
          SELECT DISTINCT ON (h.holder_name, h.ticker)
            h.holder_name, h.ticker, h.report_date, h.shares_held
          FROM institutional_holders h
          WHERE h.report_date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
            AND h.shares_held > 0
          ORDER BY h.holder_name, h.ticker, h.report_date DESC
        ),
        priced AS (
          SELECT s.*, p0.close AS price_at_report, pn.close AS price_now, pn.date AS asof_date
          FROM snapshot s
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker=s.ticker AND date<=s.report_date
                        ORDER BY date DESC LIMIT 1) p0 ON true
          JOIN LATERAL (SELECT close, date FROM eod_prices
                        WHERE ticker=s.ticker
                        ORDER BY date DESC LIMIT 1) pn ON true
          WHERE p0.close > 0
        ),
        benched AS (
          SELECT pr.*, b0.close AS bench_at_report, bn.close AS bench_now
          FROM priced pr
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker='{benchmark}' AND date<=pr.report_date
                        ORDER BY date DESC LIMIT 1) b0 ON true
          JOIN LATERAL (SELECT close FROM eod_prices
                        WHERE ticker='{benchmark}' AND date<=pr.asof_date
                        ORDER BY date DESC LIMIT 1) bn ON true
          WHERE b0.close > 0
        ),
        holder_perf AS (
          SELECT holder_name,
            SUM(((price_now/price_at_report - 1.0) - (bench_now/bench_at_report - 1.0))
                * shares_held * price_at_report)
              / NULLIF(SUM(shares_held * price_at_report), 0) AS vw_fwd_alpha
          FROM benched
          GROUP BY holder_name
          HAVING COUNT(*) >= {min_positions}
        ),
        top_holders AS (
          SELECT holder_name FROM holder_perf
          WHERE vw_fwd_alpha >= (
            SELECT percentile_cont(0.75) WITHIN GROUP (ORDER BY vw_fwd_alpha)
            FROM holder_perf
          )
        )
        SELECT
          h.ticker, f.name, f.sector, f.market_cap, f.pe_ratio,
          COUNT(DISTINCT h.holder_name)            AS smart_holders,
          STRING_AGG(DISTINCT h.holder_name, ', ') AS holders,
          e.close                                  AS price
        FROM institutional_holders h
        JOIN top_holders th ON th.holder_name = h.holder_name
        JOIN fundamentals f ON f.ticker = h.ticker
        JOIN eod_prices e   ON e.ticker = h.ticker
                           AND e.date = (SELECT MAX(date) FROM eod_prices WHERE ticker=h.ticker)
        WHERE f.is_delisted = false
          AND h.report_date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
        GROUP BY h.ticker, f.name, f.sector, f.market_cap, f.pe_ratio, e.close
        HAVING COUNT(DISTINCT h.holder_name) >= {min_smart_holders}
        ORDER BY smart_holders DESC
        LIMIT {limit}
        """,
        "params": {
            "lookback_days": [180, 365],
            "min_positions": [5, 10],
            "min_smart_holders": [2, 3, 5],
            "benchmark": ["SPY.US"],
            "limit": [25, 50],
        },
    }
}


# Strict pattern for the few string-valued params (e.g. the benchmark symbol).
# Such values must ALSO appear in their strategy's own allowlist below.
_SAFE_STR = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")


def validate_params(strategy_name: str, allowed: dict, chosen: dict) -> dict:
    """Gate every value before it is str.format()'d into a query string.

    The explorer substitutes {name} placeholders into SQL rather than binding
    parameters (interval literals and identifiers don't all bind cleanly), so
    this is the single seam where a value becomes SQL. Values are only ever
    drawn from each strategy's hardcoded ``params`` lists, so today this guard
    only ever passes -- but it makes injection structurally impossible if params
    ever come from anywhere else. Ints and floats pass as numeric literals;
    strings must both match a strict pattern AND appear in the declared
    allowlist; anything else (including bool, an int subclass) is rejected.
    """
    safe: dict = {}
    for key, value in chosen.items():
        if isinstance(value, bool):
            raise ValueError(f"{strategy_name}.{key}: bool is not a valid query value")
        if isinstance(value, (int, float)):
            safe[key] = value
        elif isinstance(value, str):
            if value not in allowed.get(key, ()):
                raise ValueError(
                    f"{strategy_name}.{key}: {value!r} is not in the declared allowlist")
            if not _SAFE_STR.match(value):
                raise ValueError(
                    f"{strategy_name}.{key}: {value!r} fails the safe-string check")
            safe[key] = value
        else:
            raise ValueError(
                f"{strategy_name}.{key}: unsupported param type {type(value).__name__}")
    return safe
