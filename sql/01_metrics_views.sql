-- =====================================================================
-- 01_metrics_views.sql   (REVISION 2)
--
-- WHAT CHANGED FROM REVISION 1, AND WHY
--
-- Revision 1 computed flow by diffing each ticker's two most recent
-- snapshots. That was built on a premise the data does not contain.
-- institutional_holders has no snapshot dimension: each holder carries
-- its own filing date, and 99.77% of (ticker, holder) pairs appear
-- exactly once. The diff produced 77,915 "initiations" against 227 real
-- comparisons, which would have read as a mass exodus in every issue.
--
-- Revision 2 reads EODHD's own per-holder `change` / `change_p`,
-- recovered by 00_fix_holders.sql. No reconstruction, no assumptions
-- about filing cadence.
--
-- FOUR PROPERTIES OF THIS DATA THAT SHAPE EVERYTHING BELOW
--
--   (a) EXITS ARE UNOBSERVABLE. The payload lists CURRENT holders only.
--       A holder who sold out entirely has no row at all. There is no
--       'exited' action and there cannot be one from this source. Any
--       "N holders exited" claim would be fabricated. Exits become
--       observable only by diffing your own observed_at vintages over
--       time -- forward-accumulating, like the analyst ratings.
--
--   (b) change_p = 0 IS OVERLOADED. It means either "no change" or "new
--       position" (prev=0 divides to zero rather than infinity). Verified
--       in the payload: Tidal Investments has change=67385,
--       currentShares=67385, change_p=0 -- an initiation. Geode has
--       change=0, currentShares=21833, change_p=0 -- genuinely unchanged.
--       Disambiguate on change_shares = shares_held, never on change_pct.
--
--   (c) TOP-N TRUNCATION -- CONFIRMED AT 20, AND IT BIASES FLOW.
--
--       Verified universe-wide: 4,532 of 5,307 tickers have EXACTLY 20
--       holders. 85% sit at a hard cap. Truncation is the normal case,
--       not an edge case.
--
--       Two consequences. First, summing pct_shares yields TOP-20
--       ownership, not total; columns say top_n_ so prose cannot promote
--       them. Second, and less obvious: at the cap, TWO mechanisms delete
--       negative flow and none delete positive flow --
--         1. a holder who exits entirely has no row (see (a));
--         2. a holder who falls below rank 20 drops off the list, and its
--            reduction goes with it.
--       So net_change_pct skews UPWARD wherever top_n_at_cap is true.
--       Accumulation readings are roughly sound; distribution readings are
--       a FLOOR -- true outflow is worse than reported. The rollup carries
--       top_n_at_cap so the findings layer can say so.
--
--   (d) MIXED FILING DATES WITHIN A TICKER -- AND IT DIFFERS BY SOURCE.
--
--       Institutions cluster on quarter-ends: 19 of 20 at 2026-03-31 in
--       the sample, one straggler. Anchoring net flow on the ticker's
--       latest report_date captures ~95% and stays a clean single-period
--       figure. inst_flow_ticker does exactly that.
--
--       Funds do NOT cluster. The same sample has 20 funds spread over
--       SIX month-ends (2025-11-30 .. 2026-05-31) with only 3 at the max.
--       Anchoring on the max date there would compute net flow from 3 of
--       20 funds and silently discard Janney Global Small Cap's +315,821
--       share build for filing at 2026-03-31 instead of 2026-05-31.
--       Staggered month-ends are how funds report; the max date is not a
--       meaningful anchor. fund_flow_ticker therefore uses a recency
--       WINDOW and carries the actual date span in the rollup so the
--       prose can state it.
--
-- Run order: 00_fix_holders.sql -> 01_metrics_views.sql -> 02_...
-- Target: PostgreSQL 16
-- =====================================================================


-- ---------------------------------------------------------------------
-- inst_flow : one row per (ticker, holder, filing date), with EODHD's
--             delta and a derived action label.
--
-- Rows with NULL change_shares are pre-migration leftovers: holders no
-- longer present in the current payload. They are stale and excluded.
-- ---------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS inst_flow_ticker CASCADE;
DROP MATERIALIZED VIEW IF EXISTS inst_flow CASCADE;

CREATE MATERIALIZED VIEW inst_flow AS
SELECT
    h.ticker,
    h.holder_name,
    h.report_date,
    h.observed_at,
    h.shares_held,
    h.pct_shares,
    h.pct_assets,
    h.change_shares,
    h.change_pct,
    (h.shares_held - h.change_shares) AS prior_shares,
    CASE
        -- (b): initiation detected on shares, never on change_pct
        WHEN h.change_shares = h.shares_held AND h.change_shares <> 0
                                        THEN 'initiated'
        WHEN h.change_shares = 0        THEN 'unchanged'
        WHEN h.change_shares > 0        THEN 'added'
        WHEN h.change_shares < 0        THEN 'trimmed'
    END AS action,
    -- (b) again: null out the change_pct that EODHD zeroed by dividing by
    -- zero, so downstream cannot read an initiation as a flat position.
    CASE
        WHEN h.change_shares = h.shares_held AND h.change_shares <> 0 THEN NULL
        ELSE h.change_pct
    END AS change_pct_clean
FROM institutional_holders h
WHERE h.change_shares IS NOT NULL;

CREATE UNIQUE INDEX inst_flow_pk    ON inst_flow (ticker, holder_name, report_date);
CREATE INDEX inst_flow_action_idx   ON inst_flow (action);
CREATE INDEX inst_flow_ticker_idx   ON inst_flow (ticker);
CREATE INDEX inst_flow_rdate_idx    ON inst_flow (report_date);


-- ---------------------------------------------------------------------
-- fund_flow : same shape against fund_holders (no pct_assets).
-- ---------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS fund_flow_ticker CASCADE;
DROP MATERIALIZED VIEW IF EXISTS fund_flow CASCADE;

CREATE MATERIALIZED VIEW fund_flow AS
SELECT
    h.ticker,
    h.holder_name,
    h.report_date,
    h.observed_at,
    h.shares_held,
    h.pct_shares,
    h.change_shares,
    h.change_pct,
    (h.shares_held - h.change_shares) AS prior_shares,
    CASE
        WHEN h.change_shares = h.shares_held AND h.change_shares <> 0
                                        THEN 'initiated'
        WHEN h.change_shares = 0        THEN 'unchanged'
        WHEN h.change_shares > 0        THEN 'added'
        WHEN h.change_shares < 0        THEN 'trimmed'
    END AS action,
    CASE
        WHEN h.change_shares = h.shares_held AND h.change_shares <> 0 THEN NULL
        ELSE h.change_pct
    END AS change_pct_clean
FROM fund_holders h
WHERE h.change_shares IS NOT NULL;

CREATE UNIQUE INDEX fund_flow_pk    ON fund_flow (ticker, holder_name, report_date);
CREATE INDEX fund_flow_action_idx   ON fund_flow (action);
CREATE INDEX fund_flow_ticker_idx   ON fund_flow (ticker);


-- ---------------------------------------------------------------------
-- inst_flow_ticker : ticker-level rollup. What the newsletter ranks on.
--
-- (d): net flow is restricted to holders filing at the ticker's LATEST
-- report_date. Stragglers still count toward ownership/concentration
-- (they hold the shares regardless) but not toward the quarterly delta.
--
-- (c): every ownership column is prefixed top_n_ so it cannot be
-- narrated as total institutional ownership.
-- ---------------------------------------------------------------------

CREATE MATERIALIZED VIEW inst_flow_ticker AS
WITH latest AS (
    SELECT ticker, max(report_date) AS latest_filing
    FROM inst_flow
    GROUP BY ticker
)
SELECT
    f.ticker,
    l.latest_filing,
    min(f.report_date)                              AS earliest_filing,
    max(f.observed_at)                              AS observed_at,
    count(*)                                        AS top_n_holders,
    (count(*) >= 20)                                AS top_n_at_cap,
    count(*) FILTER (WHERE f.report_date = l.latest_filing)
                                                    AS holders_at_latest,
    count(*) FILTER (WHERE f.report_date <> l.latest_filing)
                                                    AS holders_lagging,

    -- Ownership / concentration: all holders, any filing date.
    sum(f.shares_held)                              AS top_n_shares,
    round(sum(f.pct_shares), 4)                     AS top_n_pct_of_shares_out,
    round(max(f.pct_shares), 4)                     AS largest_holder_pct,
    round(max(f.pct_assets), 4)                     AS max_holder_conviction_pct,

    -- Flow: latest filing date only. See (d).
    sum(f.change_shares) FILTER (WHERE f.report_date = l.latest_filing)
                                                    AS net_change_shares,
    sum(f.shares_held - f.change_shares) FILTER (WHERE f.report_date = l.latest_filing)
                                                    AS prior_shares_at_latest,
    CASE WHEN sum(f.shares_held - f.change_shares)
              FILTER (WHERE f.report_date = l.latest_filing) > 0
         THEN round(
             sum(f.change_shares) FILTER (WHERE f.report_date = l.latest_filing)
             / sum(f.shares_held - f.change_shares) FILTER (WHERE f.report_date = l.latest_filing)
             * 100.0, 2)
    END                                             AS net_change_pct,

    count(*) FILTER (WHERE f.action = 'added'     AND f.report_date = l.latest_filing) AS n_added,
    count(*) FILTER (WHERE f.action = 'trimmed'   AND f.report_date = l.latest_filing) AS n_trimmed,
    count(*) FILTER (WHERE f.action = 'initiated' AND f.report_date = l.latest_filing) AS n_initiated,
    count(*) FILTER (WHERE f.action = 'unchanged' AND f.report_date = l.latest_filing) AS n_unchanged
    -- NOTE: no n_exited. See (a). Exits are not in this data.
FROM inst_flow f
JOIN latest l ON l.ticker = f.ticker
GROUP BY f.ticker, l.latest_filing;

CREATE UNIQUE INDEX inst_flow_ticker_pk  ON inst_flow_ticker (ticker);
CREATE INDEX inst_flow_ticker_net_idx    ON inst_flow_ticker (net_change_pct);
CREATE INDEX inst_flow_ticker_date_idx   ON inst_flow_ticker (latest_filing);


-- ---------------------------------------------------------------------
-- fund_flow_ticker
-- ---------------------------------------------------------------------

-- WINDOW_DAYS: funds report on staggered month-ends. 95 days captures
-- roughly the last three monthly cycles. Each fund's `change` is its own
-- change since its own prior report, so the sum is net flow across funds
-- at their RESPECTIVE latest filings -- asynchronous, and labelled as such.
CREATE MATERIALIZED VIEW fund_flow_ticker AS
WITH bounds AS (
    SELECT ticker,
           max(report_date)                  AS latest_filing,
           max(report_date) - 95             AS window_start
    FROM fund_flow
    GROUP BY ticker
)
SELECT
    f.ticker,
    b.latest_filing,
    min(f.report_date) FILTER (WHERE f.report_date >= b.window_start)
                                                    AS window_earliest_filing,
    (b.latest_filing - min(f.report_date) FILTER (WHERE f.report_date >= b.window_start))
                                                    AS filing_span_days,
    max(f.observed_at)                              AS observed_at,
    count(*)                                        AS top_n_funds,
    count(*) FILTER (WHERE f.report_date >= b.window_start) AS funds_in_window,
    count(*) FILTER (WHERE f.report_date <  b.window_start) AS funds_stale,
    sum(f.shares_held)                              AS top_n_shares,
    round(sum(f.pct_shares), 4)                     AS top_n_pct_of_shares_out,
    sum(f.change_shares) FILTER (WHERE f.report_date >= b.window_start)
                                                    AS net_change_shares,
    sum(f.shares_held - f.change_shares) FILTER (WHERE f.report_date >= b.window_start)
                                                    AS prior_shares_in_window,
    CASE WHEN sum(f.shares_held - f.change_shares)
              FILTER (WHERE f.report_date >= b.window_start) > 0
         THEN round(
             sum(f.change_shares) FILTER (WHERE f.report_date >= b.window_start)
             / sum(f.shares_held - f.change_shares) FILTER (WHERE f.report_date >= b.window_start)
             * 100.0, 2)
    END                                             AS net_change_pct,
    count(*) FILTER (WHERE f.action = 'initiated' AND f.report_date >= b.window_start) AS n_initiated,
    count(*) FILTER (WHERE f.action = 'added'     AND f.report_date >= b.window_start) AS n_added,
    count(*) FILTER (WHERE f.action = 'trimmed'   AND f.report_date >= b.window_start) AS n_trimmed
FROM fund_flow f
JOIN bounds b ON b.ticker = f.ticker
GROUP BY f.ticker, b.latest_filing, b.window_start;

CREATE UNIQUE INDEX fund_flow_ticker_pk ON fund_flow_ticker (ticker);
CREATE INDEX fund_flow_ticker_net_idx   ON fund_flow_ticker (net_change_pct);


-- ---------------------------------------------------------------------
-- price_perf : UNCHANGED from revision 1. Never affected by the
--              holders problem.
--
--  * LAG offsets are TRADING days: 21/63/252 = 1m/3m/12m.
--  * The 400-day window gives ~275 trading rows, enough for the 252 lag.
--    Do not shrink below ~380 or ret_12m silently becomes NULL.
--  * adjusted_close throughout, so splits/dividends do not fake returns.
-- ---------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS price_perf CASCADE;

CREATE MATERIALIZED VIEW price_perf AS
WITH universe AS (
    SELECT DISTINCT ticker FROM institutional_holders
),
recent AS (
    SELECT p.ticker, p.date, p.adjusted_close, p.volume
    FROM eod_prices p
    JOIN universe u ON u.ticker = p.ticker
    WHERE p.date >= (CURRENT_DATE - INTERVAL '400 days')
      AND p.adjusted_close IS NOT NULL
      AND p.adjusted_close > 0
),
lagged AS (
    SELECT
        ticker, date, adjusted_close, volume,
        LAG(adjusted_close, 1)   OVER w AS c_1d,
        LAG(adjusted_close, 5)   OVER w AS c_5d,
        LAG(adjusted_close, 21)  OVER w AS c_1m,
        LAG(adjusted_close, 63)  OVER w AS c_3m,
        LAG(adjusted_close, 252) OVER w AS c_12m,
        AVG(volume) OVER (
            PARTITION BY ticker ORDER BY date
            ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
        ) AS avg_vol_20d,
        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
    FROM recent
    WINDOW w AS (PARTITION BY ticker ORDER BY date)
)
SELECT
    l.ticker,
    l.date                                          AS as_of,
    l.adjusted_close                                AS close,
    l.volume,
    round(l.avg_vol_20d, 0)                         AS avg_vol_20d,
    CASE WHEN l.avg_vol_20d > 0
         THEN round(l.volume / l.avg_vol_20d, 2) END AS vol_ratio,
    CASE WHEN l.c_1d  > 0 THEN round((l.adjusted_close / l.c_1d  - 1) * 100.0, 2) END AS ret_1d,
    CASE WHEN l.c_5d  > 0 THEN round((l.adjusted_close / l.c_5d  - 1) * 100.0, 2) END AS ret_5d,
    CASE WHEN l.c_1m  > 0 THEN round((l.adjusted_close / l.c_1m  - 1) * 100.0, 2) END AS ret_1m,
    CASE WHEN l.c_3m  > 0 THEN round((l.adjusted_close / l.c_3m  - 1) * 100.0, 2) END AS ret_3m,
    CASE WHEN l.c_12m > 0 THEN round((l.adjusted_close / l.c_12m - 1) * 100.0, 2) END AS ret_12m
FROM lagged l
WHERE l.rn = 1;

CREATE UNIQUE INDEX price_perf_pk ON price_perf (ticker);
CREATE INDEX price_perf_ret1d_idx ON price_perf (ret_1d);
CREATE INDEX price_perf_asof_idx  ON price_perf (as_of);


-- ---------------------------------------------------------------------
-- Refresh helper. Order matters: *_ticker views read their base view.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION refresh_metrics() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY inst_flow;
    REFRESH MATERIALIZED VIEW CONCURRENTLY fund_flow;
    REFRESH MATERIALIZED VIEW CONCURRENTLY inst_flow_ticker;
    REFRESH MATERIALIZED VIEW CONCURRENTLY fund_flow_ticker;
    REFRESH MATERIALIZED VIEW CONCURRENTLY price_perf;
END;
$$;
