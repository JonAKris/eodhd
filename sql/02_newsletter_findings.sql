-- =====================================================================
-- 02_newsletter_findings.sql   (REVISION 2)
--
-- The contract between SQL and the LLM. Every number that may appear in
-- the newsletter is computed here and stored in newsletter_findings.facts.
-- The LLM reads this table and narrates it. No DB access, no arithmetic.
--
-- CHANGES FROM REVISION 1
--   * inst_accumulation / inst_distribution read EODHD's per-holder
--     `change` via inst_flow_ticker, not a reconstructed snapshot diff.
--   * No 'exited' anywhere. Exits are unobservable in this data source
--     (the payload lists current holders only). Rev 1 would have printed
--     fabricated exit counts.
--   * interval_days filters are gone -- there is no interval, because
--     there are no snapshot pairs.
--   * Ownership figures are labelled top_n_*, because the payload caps
--     holders per ticker (20 in the sample). They are NOT total
--     institutional ownership.
--   * New section: inst_ownership. Concentration and conviction, which
--     need no history and work at launch.
--
-- Run after 00_fix_holders.sql and 01_metrics_views.sql.
-- =====================================================================

DROP TABLE IF EXISTS newsletter_config CASCADE;

CREATE TABLE newsletter_config (
    key   text PRIMARY KEY,
    value numeric NOT NULL,
    note  text
);

INSERT INTO newsletter_config (key, value, note) VALUES
  ('min_price',        5.00,   'Exclude sub-$5 names from movers/breadth'),
  ('min_avg_vol_20d',  100000, 'Liquidity floor. Without it, movers is microcap noise'),
  ('min_holders',      5,      'Top-N holders required for a ticker to be rankable'),
  ('min_prior_shares', 50000,  'Baseline floor. EODHD change_p hits +1342% on a 23k base'),
  ('min_holders_at_latest', 4, 'Net flow needs enough current filers to mean anything'),
  ('top_n',            5,      'Rows per section'),
  ('top_n_movers',     10,     'Rows per movers section (advancing / declining)');

CREATE OR REPLACE FUNCTION cfg(p_key text) RETURNS numeric
LANGUAGE sql STABLE AS $$
    SELECT value FROM newsletter_config WHERE key = p_key;
$$;


-- ---------------------------------------------------------------------
-- Benchmark set for the market-overview section (DOW, S&P, Nasdaq, ...).
-- Config-driven so the symbols can change without touching the function.
-- Defaults to liquid ETF proxies, which are present in eod_prices. Swap in
-- true index tickers (e.g. GSPC.INDX, DJI.INDX) here if/when they are ingested.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS newsletter_benchmarks (
    symbol       text PRIMARY KEY,
    display_name text NOT NULL,
    sort_order   int  NOT NULL DEFAULT 100
);

INSERT INTO newsletter_benchmarks (symbol, display_name, sort_order) VALUES
  ('SPY.US', 'S&P 500 (SPY)',       10),
  ('DIA.US', 'Dow Jones (DIA)',     20),
  ('QQQ.US', 'Nasdaq 100 (QQQ)',    30),
  ('IWM.US', 'Russell 2000 (IWM)',  40)
ON CONFLICT (symbol) DO UPDATE
  SET display_name = EXCLUDED.display_name,
      sort_order   = EXCLUDED.sort_order;


-- ---------------------------------------------------------------------
-- Strategy signals. Populated by explorer/runner.py after a run: one row
-- per multi-signal ticker, carrying the strategy names that flagged it and
-- its conviction rank. build_newsletter reads this and buckets the names
-- by analyst consensus. Not dropped on reload -- it is producer-owned data,
-- not something the newsletter computes.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_signals (
    issue_date        date        NOT NULL,
    ticker            text        NOT NULL,
    signals           text[]      NOT NULL,
    signal_count      int         NOT NULL,
    is_top_conviction boolean     NOT NULL DEFAULT false,
    conviction_rank   int,
    run_ts            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_date, ticker)
);


-- ---------------------------------------------------------------------
-- SSG results. Populated by ssg_screener.py: one row per quality-growth
-- company with its price zone and the stricter is_buy verdict. build_newsletter
-- buckets these Buy/Hold/Sell. Producer-owned, so not dropped on reload.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ssg_results (
    issue_date              date    NOT NULL,
    ticker                  text    NOT NULL,
    name                    text,
    sector                  text,
    market_cap              numeric,
    current_price           numeric,
    zone                    text,
    is_buy                  boolean NOT NULL DEFAULT false,
    quality_pass            boolean NOT NULL DEFAULT false,
    buy_below               numeric,
    sell_above              numeric,
    updown_ratio            numeric,
    total_return            numeric,
    price_appreciation_cagr numeric,
    avg_yield               numeric,
    forecast_high_price     numeric,
    forecast_low_price      numeric,
    projected_eps_5yr       numeric,
    high_pe                 numeric,
    low_pe                  numeric,
    roe                     numeric,
    reasons                 text[],
    run_ts                  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issue_date, ticker)
);


DROP TABLE IF EXISTS newsletter_findings CASCADE;

CREATE TABLE newsletter_findings (
    id         bigserial PRIMARY KEY,
    issue_date date   NOT NULL,
    section    text   NOT NULL,
    rank       int    NOT NULL,
    ticker     text,
    headline   text   NOT NULL,
    facts      jsonb  NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issue_date, section, rank)
);

CREATE INDEX newsletter_findings_issue_idx ON newsletter_findings (issue_date, section, rank);


CREATE OR REPLACE FUNCTION build_newsletter(p_issue_date date DEFAULT CURRENT_DATE)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_count      integer;
    v_price_asof date;
    v_inst_asof  date;
    v_sig_date   date;
    v_ssg_date   date;
BEGIN
    DELETE FROM newsletter_findings WHERE issue_date = p_issue_date;

    SELECT max(as_of)        INTO v_price_asof FROM price_perf;
    SELECT max(latest_filing) INTO v_inst_asof FROM inst_flow_ticker;

    -- ================= provenance =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'provenance', 1, NULL,
        format('Prices as of %s. Institutional positions reflect filings dated up to %s, %s days before publication.',
               v_price_asof, v_inst_asof, (p_issue_date - v_inst_asof)),
        jsonb_build_object(
            'issue_date',             p_issue_date,
            'price_as_of',            v_price_asof,
            'institutional_as_of',    v_inst_asof,
            'institutional_lag_days', (p_issue_date - v_inst_asof),
            'universe_size',          (SELECT count(*) FROM price_perf),
            'holders_universe_size',  (SELECT count(*) FROM inst_flow_ticker),
            'analyst_ratings_rows',   (SELECT count(*) FROM analyst_ratings_history),
            'holder_coverage_note',
              'Holder data covers each ticker''s largest reported holders only, not every holder.',
            'exit_note',
              'Positions closed entirely are not reported by the data source and are not counted.'
        );

    -- ================= market_breadth =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'market_breadth', 1, NULL,
        format('Of %s liquid names in the covered universe, %s advanced and %s declined; median move %s%%.',
               b.total, b.advancers, b.decliners, b.median_ret_1d),
        jsonb_build_object(
            'scope',        'institutional-holdings universe, liquidity-filtered',
            'scope_caveat', 'This is not an index. It is a breadth measure over covered names.',
            'as_of', v_price_asof,
            'total', b.total, 'advancers', b.advancers, 'decliners', b.decliners,
            'unchanged', b.unchanged, 'advance_decline_ratio', b.ad_ratio,
            'median_ret_1d', b.median_ret_1d, 'median_ret_5d', b.median_ret_5d,
            'median_ret_1m', b.median_ret_1m, 'pct_above_zero_12m', b.pct_pos_12m)
    FROM (
        SELECT count(*) AS total,
            count(*) FILTER (WHERE ret_1d > 0) AS advancers,
            count(*) FILTER (WHERE ret_1d < 0) AS decliners,
            count(*) FILTER (WHERE ret_1d = 0) AS unchanged,
            CASE WHEN count(*) FILTER (WHERE ret_1d < 0) = 0 THEN NULL
                 ELSE round(count(*) FILTER (WHERE ret_1d > 0)::numeric
                          / count(*) FILTER (WHERE ret_1d < 0), 2) END AS ad_ratio,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1d)::numeric, 2) AS median_ret_1d,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_5d)::numeric, 2) AS median_ret_5d,
            round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1m)::numeric, 2) AS median_ret_1m,
            round(100.0 * count(*) FILTER (WHERE ret_12m > 0)
                  / NULLIF(count(*) FILTER (WHERE ret_12m IS NOT NULL), 0), 1) AS pct_pos_12m
        FROM price_perf
        WHERE ret_1d IS NOT NULL
          AND close >= cfg('min_price') AND avg_vol_20d >= cfg('min_avg_vol_20d')
    ) b
    WHERE b.total > 0;

    -- ================= movers_up =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'movers_up',
        row_number() OVER (ORDER BY p.ret_1d DESC), p.ticker,
        format('%s (%s) +%s%% on %sx average volume.',
               COALESCE(f.name, p.ticker), p.ticker, p.ret_1d, COALESCE(p.vol_ratio, 0)),
        jsonb_build_object('ticker', p.ticker, 'name', f.name, 'sector', f.sector,
            'as_of', p.as_of, 'close', p.close, 'ret_1d', p.ret_1d, 'ret_5d', p.ret_5d,
            'ret_1m', p.ret_1m, 'ret_3m', p.ret_3m, 'ret_12m', p.ret_12m,
            'volume', p.volume, 'avg_vol_20d', p.avg_vol_20d, 'vol_ratio', p.vol_ratio,
            'market_cap', f.market_cap)
    FROM price_perf p
    LEFT JOIN fundamentals f ON f.ticker = p.ticker
    WHERE p.ret_1d > 0                 -- empty section beats a mislabelled one
      AND p.close >= cfg('min_price') AND p.avg_vol_20d >= cfg('min_avg_vol_20d')
    ORDER BY p.ret_1d DESC LIMIT cfg('top_n')::int;

    -- ================= movers_down =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'movers_down',
        row_number() OVER (ORDER BY p.ret_1d ASC), p.ticker,
        format('%s (%s) %s%% on %sx average volume.',
               COALESCE(f.name, p.ticker), p.ticker, p.ret_1d, COALESCE(p.vol_ratio, 0)),
        jsonb_build_object('ticker', p.ticker, 'name', f.name, 'sector', f.sector,
            'as_of', p.as_of, 'close', p.close, 'ret_1d', p.ret_1d, 'ret_5d', p.ret_5d,
            'ret_1m', p.ret_1m, 'ret_3m', p.ret_3m, 'ret_12m', p.ret_12m,
            'volume', p.volume, 'avg_vol_20d', p.avg_vol_20d, 'vol_ratio', p.vol_ratio,
            'market_cap', f.market_cap)
    FROM price_perf p
    LEFT JOIN fundamentals f ON f.ticker = p.ticker
    WHERE p.ret_1d < 0
      AND p.close >= cfg('min_price') AND p.avg_vol_20d >= cfg('min_avg_vol_20d')
    ORDER BY p.ret_1d ASC LIMIT cfg('top_n')::int;

    -- ================= inst_accumulation =================
    -- Reads EODHD's own per-holder change. No exit counts: unobservable.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'inst_accumulation',
        row_number() OVER (ORDER BY t.net_change_pct DESC), t.ticker,
        format('%s (%s): reported institutional share count rose %s%% at the %s filing date. %s holders added, %s initiated new positions, %s left unchanged.',
               COALESCE(f.name, t.ticker), t.ticker, t.net_change_pct,
               t.latest_filing, t.n_added, t.n_initiated, t.n_unchanged),
        jsonb_build_object(
            'ticker', t.ticker, 'name', f.name, 'sector', f.sector,
            'latest_filing', t.latest_filing,
            'holders_at_latest', t.holders_at_latest,
            'holders_lagging',   t.holders_lagging,
            'net_change_shares', t.net_change_shares,
            'net_change_pct',    t.net_change_pct,
            'prior_shares_at_latest', t.prior_shares_at_latest,
            'top_n_holders', t.top_n_holders,
            'top_n_pct_of_shares_out', t.top_n_pct_of_shares_out,
            'top_n_at_cap', t.top_n_at_cap,
            'coverage_note', 'Covers the largest reported holders only. Not total institutional ownership.',
            'bias_note', 'Where the holder list is capped, holders that exited or fell out of the largest-holder list are not reported, so reductions are undercounted. This figure understates selling.',
            'n_added', t.n_added, 'n_trimmed', t.n_trimmed,
            'n_initiated', t.n_initiated, 'n_unchanged', t.n_unchanged,
            'ret_3m', p.ret_3m, 'ret_12m', p.ret_12m,
            'top_movers', (
                SELECT jsonb_agg(x) FROM (
                    SELECT jsonb_build_object('holder', i.holder_name, 'action', i.action,
                        'change_shares', i.change_shares, 'change_pct', i.change_pct_clean,
                        'shares_held', i.shares_held,
                        'pct_of_holder_portfolio', i.pct_assets) AS x
                    FROM inst_flow i
                    WHERE i.ticker = t.ticker AND i.report_date = t.latest_filing
                      AND i.action IN ('added','initiated')
                    ORDER BY i.change_shares DESC LIMIT 3
                ) s))
    FROM inst_flow_ticker t
    LEFT JOIN fundamentals f ON f.ticker = t.ticker
    LEFT JOIN price_perf   p ON p.ticker = t.ticker
    WHERE t.net_change_pct IS NOT NULL
      AND t.top_n_holders          >= cfg('min_holders')
      AND t.holders_at_latest      >= cfg('min_holders_at_latest')
      AND t.prior_shares_at_latest >= cfg('min_prior_shares')
    ORDER BY t.net_change_pct DESC LIMIT cfg('top_n')::int;

    -- ================= inst_distribution =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'inst_distribution',
        row_number() OVER (ORDER BY t.net_change_pct ASC), t.ticker,
        format('%s (%s): reported institutional share count fell %s%% at the %s filing date. %s holders trimmed, %s added, %s left unchanged.',
               COALESCE(f.name, t.ticker), t.ticker, abs(t.net_change_pct),
               t.latest_filing, t.n_trimmed, t.n_added, t.n_unchanged),
        jsonb_build_object(
            'ticker', t.ticker, 'name', f.name, 'sector', f.sector,
            'latest_filing', t.latest_filing,
            'holders_at_latest', t.holders_at_latest,
            'holders_lagging',   t.holders_lagging,
            'net_change_shares', t.net_change_shares,
            'net_change_pct',    t.net_change_pct,
            'prior_shares_at_latest', t.prior_shares_at_latest,
            'top_n_holders', t.top_n_holders,
            'top_n_pct_of_shares_out', t.top_n_pct_of_shares_out,
            'top_n_at_cap', t.top_n_at_cap,
            'coverage_note', 'Covers the largest reported holders only. Positions closed entirely are not reported.',
            'bias_note', 'This is a lower bound on selling. Holders that exited or fell out of the largest-holder list are not reported, so the actual reduction is at least this large and probably larger.',
            'n_added', t.n_added, 'n_trimmed', t.n_trimmed,
            'n_initiated', t.n_initiated, 'n_unchanged', t.n_unchanged,
            'ret_3m', p.ret_3m, 'ret_12m', p.ret_12m,
            'top_movers', (
                SELECT jsonb_agg(x) FROM (
                    SELECT jsonb_build_object('holder', i.holder_name, 'action', i.action,
                        'change_shares', i.change_shares, 'change_pct', i.change_pct_clean,
                        'shares_held', i.shares_held,
                        'pct_of_holder_portfolio', i.pct_assets) AS x
                    FROM inst_flow i
                    WHERE i.ticker = t.ticker AND i.report_date = t.latest_filing
                      AND i.action = 'trimmed'
                    ORDER BY i.change_shares ASC LIMIT 3
                ) s))
    FROM inst_flow_ticker t
    LEFT JOIN fundamentals f ON f.ticker = t.ticker
    LEFT JOIN price_perf   p ON p.ticker = t.ticker
    WHERE t.net_change_pct IS NOT NULL
      AND t.top_n_holders          >= cfg('min_holders')
      AND t.holders_at_latest      >= cfg('min_holders_at_latest')
      AND t.prior_shares_at_latest >= cfg('min_prior_shares')
    ORDER BY t.net_change_pct ASC LIMIT cfg('top_n')::int;

    -- ================= inst_ownership =================
    -- NEW in rev 2. Concentration and conviction. Needs no history, no
    -- deltas, no snapshots. Works at launch and is not affected by any
    -- of the four data traps except top-N truncation, which is labelled.
    --
    -- pct_assets = share of the HOLDER's own portfolio in this name.
    -- A high value is a conviction signal and is cross-sectional, not
    -- temporal, so it is immune to the whole snapshot problem.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'inst_ownership',
        row_number() OVER (ORDER BY t.max_holder_conviction_pct DESC), t.ticker,
        format('%s (%s): %s reported institutional holders; largest holds %s%% of shares outstanding. One holder allocates %s%% of its own portfolio to the position.',
               COALESCE(f.name, t.ticker), t.ticker, t.top_n_holders,
               t.largest_holder_pct, t.max_holder_conviction_pct),
        jsonb_build_object(
            'ticker', t.ticker, 'name', f.name, 'sector', f.sector,
            'latest_filing', t.latest_filing,
            'top_n_holders', t.top_n_holders,
            'top_n_pct_of_shares_out', t.top_n_pct_of_shares_out,
            'largest_holder_pct', t.largest_holder_pct,
            'max_holder_conviction_pct', t.max_holder_conviction_pct,
            'top_n_at_cap', t.top_n_at_cap,
            'coverage_note', 'Covers the largest reported holders only, not every institutional holder.',
            'conviction_selection_note', 'The largest-holder list is ranked by position size, not by conviction. A small holder with a very concentrated position may be absent. This is the most concentrated among reported holders, not necessarily overall.',
            'conviction_note', 'Portfolio allocation is the share of that holder''s own reported portfolio in this security.',
            'ret_3m', p.ret_3m, 'ret_12m', p.ret_12m,
            'most_concentrated', (
                SELECT jsonb_agg(x) FROM (
                    SELECT jsonb_build_object('holder', i.holder_name,
                        'pct_of_holder_portfolio', i.pct_assets,
                        'pct_of_shares_out', i.pct_shares,
                        'shares_held', i.shares_held) AS x
                    FROM inst_flow i
                    WHERE i.ticker = t.ticker AND i.pct_assets IS NOT NULL
                    ORDER BY i.pct_assets DESC LIMIT 3
                ) s))
    FROM inst_flow_ticker t
    LEFT JOIN fundamentals f ON f.ticker = t.ticker
    LEFT JOIN price_perf   p ON p.ticker = t.ticker
    WHERE t.max_holder_conviction_pct IS NOT NULL
      AND t.top_n_holders >= cfg('min_holders')
    ORDER BY t.max_holder_conviction_pct DESC LIMIT cfg('top_n')::int;

    -- ================= fund_flow_notable =================
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'fund_flow_notable',
        row_number() OVER (ORDER BY abs(t.net_change_pct) DESC), t.ticker,
        format('%s (%s): fund-held shares %s %s%% across %s reported funds filing between %s and %s.',
               COALESCE(f.name, t.ticker), t.ticker,
               CASE WHEN t.net_change_pct >= 0 THEN 'rose' ELSE 'fell' END,
               abs(t.net_change_pct), t.funds_in_window,
               t.window_earliest_filing, t.latest_filing),
        jsonb_build_object('ticker', t.ticker, 'name', f.name, 'sector', f.sector,
            'latest_filing', t.latest_filing,
            'window_earliest_filing', t.window_earliest_filing,
            'filing_span_days', t.filing_span_days,
            'top_n_funds', t.top_n_funds,
            'funds_in_window', t.funds_in_window, 'funds_stale', t.funds_stale,
            'net_change_shares', t.net_change_shares,
            'net_change_pct', t.net_change_pct,
            'prior_shares_in_window', t.prior_shares_in_window,
            'top_n_pct_of_shares_out', t.top_n_pct_of_shares_out,
            'coverage_note', 'Covers the largest reported fund holders only.',
            'asynchrony_note', 'Funds report on staggered month-ends. Each change is measured against that fund''s own prior report, so this aggregates across different reporting periods.',
            'n_initiated', t.n_initiated, 'n_added', t.n_added, 'n_trimmed', t.n_trimmed)
    FROM fund_flow_ticker t
    LEFT JOIN fundamentals f ON f.ticker = t.ticker
    WHERE t.net_change_pct IS NOT NULL
      AND t.top_n_funds            >= cfg('min_holders')
      AND t.funds_in_window        >= 3
      AND t.prior_shares_in_window >= cfg('min_prior_shares')
    ORDER BY abs(t.net_change_pct) DESC LIMIT cfg('top_n')::int;

    -- ================= insider_activity =================
    -- Codes P and S only. A/M/G are grants, option exercises and gifts:
    -- compensation events, not conviction.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'insider_activity',
        row_number() OVER (ORDER BY abs(agg.net_value) DESC), agg.ticker,
        format('%s (%s): %s open-market insider %s across %s %s over the last 30 days, net %s of $%s.',
               COALESCE(f.name, agg.ticker), agg.ticker, agg.n_txns,
               CASE WHEN agg.n_txns = 1 THEN 'transaction' ELSE 'transactions' END,
               agg.n_insiders,
               CASE WHEN agg.n_insiders = 1 THEN 'insider' ELSE 'insiders' END,
               CASE WHEN agg.net_value >= 0 THEN 'buying' ELSE 'selling' END,
               to_char(abs(agg.net_value), 'FM999,999,999,990')),
        jsonb_build_object('ticker', agg.ticker, 'name', f.name, 'sector', f.sector,
            'window_days', 30, 'n_txns', agg.n_txns,
            'n_buys', agg.n_buys, 'n_sells', agg.n_sells, 'n_insiders', agg.n_insiders,
            'buy_value', agg.buy_value, 'sell_value', agg.sell_value,
            'net_value', agg.net_value, 'earliest', agg.earliest, 'latest', agg.latest)
    FROM (
        SELECT it.ticker, count(*) AS n_txns,
            count(*) FILTER (WHERE it.transaction_code='P') AS n_buys,
            count(*) FILTER (WHERE it.transaction_code='S') AS n_sells,
            count(DISTINCT it.owner_name) AS n_insiders,
            COALESCE(sum(it.value) FILTER (WHERE it.transaction_code='P'),0) AS buy_value,
            COALESCE(sum(it.value) FILTER (WHERE it.transaction_code='S'),0) AS sell_value,
            COALESCE(sum(it.value) FILTER (WHERE it.transaction_code='P'),0)
              - COALESCE(sum(it.value) FILTER (WHERE it.transaction_code='S'),0) AS net_value,
            min(it.transaction_date) AS earliest, max(it.transaction_date) AS latest
        FROM insider_transactions it
        WHERE it.transaction_code IN ('P','S')
          AND it.transaction_date >= (p_issue_date - INTERVAL '30 days')
          AND it.value IS NOT NULL
        GROUP BY it.ticker HAVING count(*) >= 2
    ) agg
    LEFT JOIN fundamentals f ON f.ticker = agg.ticker
    ORDER BY abs(agg.net_value) DESC LIMIT cfg('top_n')::int;

    -- ================= analyst_consensus =================
    -- Empty until the flatten cron has run on two distinct dates.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'analyst_consensus',
        row_number() OVER (ORDER BY abs(d.drift) DESC), d.ticker,
        format('%s (%s): consensus rating moved from %s to %s between %s and %s (scale: 5 = strong buy).',
               COALESCE(f.name, d.ticker), d.ticker, d.prev_rating, d.curr_rating,
               d.prev_date, d.curr_date),
        jsonb_build_object('ticker', d.ticker, 'name', f.name, 'sector', f.sector,
            'scale_note', '5 = strong buy, 1 = strong sell. Rising rating = improving consensus.',
            'curr_date', d.curr_date, 'prev_date', d.prev_date,
            'curr_rating', d.curr_rating, 'prev_rating', d.prev_rating, 'drift', d.drift,
            'curr_target_price', d.curr_target, 'prev_target_price', d.prev_target,
            'close', p.close,
            'target_vs_close_pct',
                CASE WHEN p.close > 0 THEN round((d.curr_target/p.close - 1)*100.0, 2) END,
            'strong_buy', d.strong_buy, 'buy', d.buy, 'hold', d.hold,
            'sell', d.sell, 'strong_sell', d.strong_sell)
    FROM (
        SELECT c.ticker, c.date AS curr_date, pr.date AS prev_date,
            c.rating AS curr_rating, pr.rating AS prev_rating,
            round(c.rating - pr.rating, 4) AS drift,
            c.target_price AS curr_target, pr.target_price AS prev_target,
            c.strong_buy, c.buy, c.hold, c.sell, c.strong_sell
        FROM (SELECT DISTINCT ON (ticker) * FROM analyst_ratings_history
              WHERE rating IS NOT NULL ORDER BY ticker, date DESC) c
        JOIN LATERAL (
            SELECT * FROM analyst_ratings_history a
            WHERE a.ticker = c.ticker AND a.date < c.date AND a.rating IS NOT NULL
            ORDER BY a.date DESC LIMIT 1) pr ON true
        WHERE abs(c.rating - pr.rating) >= 0.05
    ) d
    LEFT JOIN fundamentals f ON f.ticker = d.ticker
    LEFT JOIN price_perf   p ON p.ticker = d.ticker
    ORDER BY abs(d.drift) DESC LIMIT cfg('top_n')::int;

    -- ================= market_holidays =================
    -- Opening-paragraph context: is the US market closed today, and when is
    -- the next closure? Parsed defensively from exchange_details.holidays,
    -- whose sub-object key spellings vary across EODHD payloads.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'market_holidays',
        row_number() OVER (ORDER BY h.hdate),
        NULL,
        CASE WHEN h.hdate = p_issue_date
             THEN format('US markets are closed today for %s.', h.name)
             ELSE format('Next US market holiday: %s on %s (%s days away).',
                         h.name, h.hdate, (h.hdate - p_issue_date)) END,
        jsonb_build_object(
            'exchange', 'US', 'name', h.name, 'type', h.htype, 'date', h.hdate,
            'is_today', (h.hdate = p_issue_date),
            'days_away', (h.hdate - p_issue_date))
    FROM (
        SELECT
            COALESCE(v.value->>'Holiday', v.value->>'Name',
                     v.value->>'holiday', v.key)                 AS name,
            COALESCE(v.value->>'Type', v.value->>'type')         AS htype,
            NULLIF(COALESCE(v.value->>'Date', v.value->>'date'), '')::date AS hdate
        FROM exchange_details ed
        CROSS JOIN LATERAL jsonb_each(ed.holidays) AS v(key, value)
        WHERE ed.exchange_code = 'US'
    ) h
    -- today's closure, plus the single nearest upcoming one
    WHERE h.hdate = p_issue_date
       OR h.hdate = (SELECT min(x.hdate) FROM (
               SELECT NULLIF(COALESCE(v.value->>'Date', v.value->>'date'), '')::date AS hdate
               FROM exchange_details ed
               CROSS JOIN LATERAL jsonb_each(ed.holidays) AS v(key, value)
               WHERE ed.exchange_code = 'US'
           ) x WHERE x.hdate > p_issue_date)
    ORDER BY h.hdate;

    -- ================= earnings_reports =================
    -- Companies reporting on the issue date. Actuals vs estimate when the
    -- print has landed, otherwise the scheduled session. Ranked by size so
    -- the most consequential names lead the paragraph.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'earnings_reports',
        row_number() OVER (ORDER BY COALESCE(f.market_cap, 0) DESC),
        ec.ticker,
        CASE WHEN ec.eps_actual IS NOT NULL THEN
            format('%s (%s) reported EPS of %s vs %s estimate (%s%% surprise), %s.',
                   COALESCE(f.name, ec.ticker), ec.ticker,
                   to_char(ec.eps_actual,   'FM999990.00'),
                   to_char(ec.eps_estimate, 'FM999990.00'),
                   to_char(ec.surprise_pct, 'FM999990.0'),
                   lower(COALESCE(ec.before_after_market, 'timing n/a')))
        ELSE
            format('%s (%s) is scheduled to report %s; consensus EPS %s.',
                   COALESCE(f.name, ec.ticker), ec.ticker,
                   lower(COALESCE(ec.before_after_market, 'today')),
                   COALESCE(to_char(ec.eps_estimate, 'FM999990.00'), 'n/a'))
        END,
        jsonb_build_object(
            'ticker', ec.ticker, 'name', f.name, 'sector', f.sector,
            'market_cap', f.market_cap, 'report_date', ec.report_date,
            'session', ec.before_after_market,
            'eps_actual', ec.eps_actual, 'eps_estimate', ec.eps_estimate,
            'eps_difference', ec.eps_difference, 'surprise_pct', ec.surprise_pct,
            'status', CASE WHEN ec.eps_actual IS NOT NULL THEN 'reported' ELSE 'scheduled' END)
    FROM earnings_calendar ec
    LEFT JOIN fundamentals f ON f.ticker = ec.ticker
    WHERE ec.report_date = p_issue_date
    ORDER BY COALESCE(f.market_cap, 0) DESC
    LIMIT (cfg('top_n') * 2)::int;

    -- ================= index_overview =================
    -- The major-markets paragraph. Returns are computed straight from
    -- eod_prices for the configured benchmarks, independent of price_perf
    -- (whose universe is institutional-holdings names and excludes ETFs).
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'index_overview', b.sort_order / 10, b.symbol,
        format('%s: %s, %s%% today (%s%% 1m, %s%% YTD).',
               b.display_name, to_char(b.close, 'FM999990.00'),
               CASE WHEN b.ret_1d  >= 0 THEN '+' || b.ret_1d  ELSE b.ret_1d::text  END,
               CASE WHEN b.ret_1m  >= 0 THEN '+' || b.ret_1m  ELSE b.ret_1m::text  END,
               CASE WHEN b.ret_ytd >= 0 THEN '+' || b.ret_ytd ELSE b.ret_ytd::text END),
        jsonb_build_object(
            'symbol', b.symbol, 'display_name', b.display_name,
            'as_of', b.as_of, 'close', b.close,
            'ret_1d', b.ret_1d, 'ret_5d', b.ret_5d, 'ret_1m', b.ret_1m,
            'ret_ytd', b.ret_ytd, 'ret_12m', b.ret_12m)
    FROM (
        SELECT nb.symbol, nb.display_name, nb.sort_order, p.date AS as_of, p.close,
            round((p.close / c_1d  - 1) * 100.0, 2) AS ret_1d,
            round((p.close / c_5d  - 1) * 100.0, 2) AS ret_5d,
            round((p.close / c_1m  - 1) * 100.0, 2) AS ret_1m,
            round((p.close / c_ytd - 1) * 100.0, 2) AS ret_ytd,
            round((p.close / c_12m - 1) * 100.0, 2) AS ret_12m
        FROM newsletter_benchmarks nb
        JOIN LATERAL (
            SELECT date, close FROM eod_prices e
            WHERE e.ticker = nb.symbol AND e.close IS NOT NULL AND e.close > 0
            ORDER BY e.date DESC LIMIT 1
        ) p ON true
        LEFT JOIN LATERAL (SELECT close FROM eod_prices e WHERE e.ticker = nb.symbol AND e.date <  p.date                          AND e.close > 0 ORDER BY e.date DESC LIMIT 1) x1d  ON true
        LEFT JOIN LATERAL (SELECT close FROM eod_prices e WHERE e.ticker = nb.symbol AND e.date <= p.date - 7                       AND e.close > 0 ORDER BY e.date DESC LIMIT 1) x5d  ON true
        LEFT JOIN LATERAL (SELECT close FROM eod_prices e WHERE e.ticker = nb.symbol AND e.date <= p.date - 31                      AND e.close > 0 ORDER BY e.date DESC LIMIT 1) x1m  ON true
        LEFT JOIN LATERAL (SELECT close FROM eod_prices e WHERE e.ticker = nb.symbol AND e.date <  date_trunc('year', p.date)::date AND e.close > 0 ORDER BY e.date DESC LIMIT 1) xytd ON true
        LEFT JOIN LATERAL (SELECT close FROM eod_prices e WHERE e.ticker = nb.symbol AND e.date <= p.date - 365                     AND e.close > 0 ORDER BY e.date DESC LIMIT 1) x12m ON true
        CROSS JOIN LATERAL (SELECT x1d.close AS c_1d, x5d.close AS c_5d, x1m.close AS c_1m,
                                   xytd.close AS c_ytd, x12m.close AS c_12m) c
    ) b
    ORDER BY b.sort_order;

    -- ================= strategy_picks =================
    -- The stocks the explorer's strategies flagged, bucketed Buy/Hold/Sell by
    -- analyst consensus. verdict lives in facts so the renderer can split the
    -- one section into three columns. Consensus rating scale: 5 = strong buy.
    -- Names with no analyst coverage fall into the neutral Hold bucket, flagged
    -- rated=false so the renderer/LLM can say so rather than implying a call.
    SELECT max(issue_date) INTO v_sig_date
    FROM strategy_signals WHERE issue_date <= p_issue_date;

    IF v_sig_date IS NULL THEN
        -- No explorer run to classify. Emit a single status note, not an
        -- empty section, so the renderer can say why the picks are missing.
        INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
        VALUES (p_issue_date, 'strategy_picks', 1, NULL,
            'No strategy run available to classify: the explorer has not persisted signals on or before this issue date.',
            jsonb_build_object('status', 'no_run', 'signals_as_of', NULL));
    ELSE
        WITH latest_rating AS (
            SELECT DISTINCT ON (ticker) ticker, date, rating, target_price,
                   strong_buy, buy, hold, sell, strong_sell
            FROM analyst_ratings_history
            WHERE rating IS NOT NULL
            ORDER BY ticker, date DESC
        ),
        picks AS (
            SELECT s.ticker, s.signals, s.signal_count,
                   s.is_top_conviction, s.conviction_rank,
                   r.rating, r.target_price,
                   r.strong_buy, r.buy, r.hold, r.sell, r.strong_sell,
                   (r.rating IS NOT NULL) AS rated,
                   CASE WHEN r.rating IS NULL THEN 'hold'   -- neutral until covered
                        WHEN r.rating >= 3.5  THEN 'buy'
                        WHEN r.rating >= 2.5  THEN 'hold'
                        ELSE 'sell' END       AS verdict,
                   f.name, f.sector,
                   p.close, p.ret_1d, p.ret_3m, p.ret_12m
            FROM strategy_signals s
            LEFT JOIN latest_rating r ON r.ticker = s.ticker
            LEFT JOIN fundamentals  f ON f.ticker = s.ticker
            LEFT JOIN price_perf    p ON p.ticker = s.ticker
            WHERE s.issue_date = v_sig_date
        ),
        ranked AS (
            SELECT *,
                CASE verdict WHEN 'buy' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END AS vpri,
                row_number() OVER (
                    PARTITION BY verdict
                    ORDER BY is_top_conviction DESC,
                             COALESCE(conviction_rank, 2147483647),
                             signal_count DESC, ticker
                ) AS bucket_rank
            FROM picks
        )
        INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
        SELECT p_issue_date, 'strategy_picks',
            row_number() OVER (ORDER BY vpri, is_top_conviction DESC,
                                        COALESCE(conviction_rank, 2147483647),
                                        signal_count DESC, ticker),
            ticker,
            CASE WHEN rated THEN
                format('%s (%s): %s on analyst consensus (%s/5 across %s ratings); flagged by %s strateg%s.',
                       COALESCE(name, ticker), ticker, initcap(verdict),
                       to_char(rating, 'FM90.00'),
                       (COALESCE(strong_buy,0)+COALESCE(buy,0)+COALESCE(hold,0)+COALESCE(sell,0)+COALESCE(strong_sell,0)),
                       signal_count, CASE WHEN signal_count = 1 THEN 'y' ELSE 'ies' END)
            ELSE
                format('%s (%s): Hold (no analyst coverage); flagged by %s strateg%s: %s.',
                       COALESCE(name, ticker), ticker, signal_count,
                       CASE WHEN signal_count = 1 THEN 'y' ELSE 'ies' END,
                       array_to_string(signals, ', '))
            END,
            jsonb_build_object(
                'ticker', ticker, 'name', name, 'sector', sector,
                'verdict', verdict, 'rated', rated,
                'signals_as_of', v_sig_date,
                'signals_lag_days', (p_issue_date - v_sig_date),
                'signals', to_jsonb(signals), 'signal_count', signal_count,
                'is_top_conviction', is_top_conviction, 'conviction_rank', conviction_rank,
                'consensus_rating', rating,
                'consensus_scale_note', '5 = strong buy, 1 = strong sell.',
                'n_ratings', (COALESCE(strong_buy,0)+COALESCE(buy,0)+COALESCE(hold,0)+COALESCE(sell,0)+COALESCE(strong_sell,0)),
                'strong_buy', strong_buy, 'buy', buy, 'hold', hold,
                'sell', sell, 'strong_sell', strong_sell,
                'target_price', target_price, 'close', close,
                'target_vs_close_pct',
                    CASE WHEN close > 0 AND target_price IS NOT NULL
                         THEN round((target_price/close - 1)*100.0, 2) END,
                'ret_1d', ret_1d, 'ret_3m', ret_3m, 'ret_12m', ret_12m,
                'bucketing_note', 'Buy/Hold/Sell reflect analyst consensus, not the strategies that surfaced the name. Unrated names default to Hold.')
        FROM ranked
        WHERE bucket_rank <= (cfg('top_n') * 2)::int;
    END IF;

    -- ================= ssg_picks =================
    -- The Stock Selection Guide names, bucketed Buy/Hold/Sell. Buy is the
    -- screener's stricter is_buy verdict (buy zone AND >=3:1 up/down AND total
    -- return clears the size hurdle); Sell is the price sell zone; everything
    -- else quality-passed -- including buy-zone near-misses -- is Hold. verdict
    -- lives in facts so the renderer splits the one section into three columns.
    SELECT max(issue_date) INTO v_ssg_date
    FROM ssg_results WHERE issue_date <= p_issue_date;

    IF v_ssg_date IS NULL THEN
        INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
        VALUES (p_issue_date, 'ssg_picks', 1, NULL,
            'No SSG screen available to classify: ssg_screener.py has not persisted results on or before this issue date.',
            jsonb_build_object('status', 'no_run', 'ssg_as_of', NULL));
    ELSE
        WITH picks AS (
            SELECT g.*,
                CASE WHEN g.is_buy            THEN 'buy'
                     WHEN g.zone = 'SELL'     THEN 'sell'
                     ELSE 'hold' END AS verdict
            FROM ssg_results g
            WHERE g.issue_date = v_ssg_date
              AND g.quality_pass
        ),
        ranked AS (
            SELECT *,
                CASE verdict WHEN 'buy' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END AS vpri,
                row_number() OVER (
                    PARTITION BY verdict
                    ORDER BY total_return DESC NULLS LAST, ticker
                ) AS bucket_rank
            FROM picks
        )
        INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
        SELECT p_issue_date, 'ssg_picks',
            row_number() OVER (ORDER BY vpri, total_return DESC NULLS LAST, ticker),
            ticker,
            CASE verdict
              WHEN 'buy' THEN
                format('%s (%s): SSG Buy -- %s up/down, %s%% projected total return; buy below %s (now %s).',
                       COALESCE(name, ticker), ticker,
                       COALESCE(to_char(updown_ratio, 'FM990.0') || ':1', 'n/a'),
                       to_char(COALESCE(total_return, 0) * 100.0, 'FM990.0'),
                       to_char(buy_below, 'FM999990.00'), to_char(current_price, 'FM999990.00'))
              WHEN 'sell' THEN
                format('%s (%s): SSG Sell -- price in the sell zone (above %s; now %s).',
                       COALESCE(name, ticker), ticker,
                       to_char(sell_above, 'FM999990.00'), to_char(current_price, 'FM999990.00'))
              ELSE
                format('%s (%s): SSG Hold -- quality-growth name in the %s zone; %s%% projected total return.',
                       COALESCE(name, ticker), ticker, lower(COALESCE(zone, 'unpriced')),
                       to_char(COALESCE(total_return, 0) * 100.0, 'FM990.0'))
            END,
            jsonb_build_object(
                'ticker', ticker, 'name', name, 'sector', sector,
                'verdict', verdict, 'zone', zone, 'is_buy', is_buy,
                'quality_pass', quality_pass, 'market_cap', market_cap,
                'ssg_as_of', v_ssg_date, 'ssg_lag_days', (p_issue_date - v_ssg_date),
                'current_price', current_price, 'buy_below', buy_below, 'sell_above', sell_above,
                'updown_ratio', updown_ratio, 'total_return', total_return,
                'price_appreciation_cagr', price_appreciation_cagr, 'avg_yield', avg_yield,
                'forecast_high_price', forecast_high_price, 'forecast_low_price', forecast_low_price,
                'projected_eps_5yr', projected_eps_5yr, 'high_pe', high_pe, 'low_pe', low_pe, 'roe', roe,
                'reasons', to_jsonb(reasons),
                'return_note', 'total_return and CAGR are fractions (0.15 = 15%). up/down is a ratio.',
                'bucketing_note', 'Buy = SSG is_buy (buy zone, >=3:1 up/down, return over size hurdle). Sell = price sell zone. Hold = other quality-passed names, including buy-zone near-misses.')
        FROM ranked
        WHERE bucket_rank <= (cfg('top_n') * 2)::int;
    END IF;

    -- ================= news_recap =================
    -- The day's financial-news recap. Most recent first, one row per distinct
    -- headline, tone bucketed from the stored polarity so the LLM narrates a
    -- label rather than re-deriving one.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'news_recap',
        row_number() OVER (ORDER BY r.published_at DESC), r.ticker,
        r.title,
        jsonb_build_object(
            'title', r.title, 'ticker', r.ticker, 'symbols', r.symbols,
            'tags', r.tags, 'published_at', r.published_at, 'link', r.link,
            'sentiment_polarity', r.sentiment_polarity,
            'tone', CASE WHEN r.sentiment_polarity >=  0.15 THEN 'positive'
                         WHEN r.sentiment_polarity <= -0.15 THEN 'negative'
                         ELSE 'neutral' END)
    FROM (
        SELECT DISTINCT ON (n.title)
               n.title, n.ticker, n.symbols, n.tags, n.published_at, n.link,
               n.sentiment_polarity
        FROM news n
        WHERE n.published_at >= (p_issue_date::timestamptz - interval '36 hours')
          AND n.title IS NOT NULL
        ORDER BY n.title, n.published_at DESC
    ) r
    ORDER BY r.published_at DESC
    LIMIT (cfg('top_n') * 2)::int;

    -- ================= movers_advancing / movers_declining =================
    -- Top gainers and losers in the liquid universe (price_perf already excludes
    -- ETFs and non-holdings names). The min_price / min_avg_vol_20d floors keep
    -- this out of microcap-noise territory -- see the notes on those config keys.
    -- Ranked by 1-day return, volume breaking ties so the "most active" of two
    -- equal movers sorts first.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'movers_advancing',
        row_number() OVER (ORDER BY pp.ret_1d DESC, pp.volume DESC),
        pp.ticker,
        format('%s (%s) +%s%% on %s shares.',
               coalesce(f.name, pp.ticker), pp.ticker, pp.ret_1d,
               to_char(pp.volume, 'FM999,999,999,999')),
        jsonb_build_object(
            'ticker', pp.ticker, 'name', f.name, 'sector', f.sector,
            'as_of', pp.as_of, 'close', pp.close, 'ret_1d', pp.ret_1d,
            'ret_5d', pp.ret_5d, 'volume', pp.volume, 'avg_vol_20d', pp.avg_vol_20d,
            'vol_ratio', pp.vol_ratio, 'dollar_volume', round(pp.close * pp.volume))
    FROM price_perf pp
    LEFT JOIN fundamentals f ON f.ticker = pp.ticker
    WHERE pp.ret_1d IS NOT NULL AND pp.ret_1d > 0
      AND pp.close       >= cfg('min_price')
      AND pp.avg_vol_20d >= cfg('min_avg_vol_20d')
      AND coalesce(f.is_delisted, false) = false
    ORDER BY pp.ret_1d DESC, pp.volume DESC
    LIMIT cfg('top_n_movers')::int;

    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'movers_declining',
        row_number() OVER (ORDER BY pp.ret_1d ASC, pp.volume DESC),
        pp.ticker,
        format('%s (%s) %s%% on %s shares.',
               coalesce(f.name, pp.ticker), pp.ticker, pp.ret_1d,
               to_char(pp.volume, 'FM999,999,999,999')),
        jsonb_build_object(
            'ticker', pp.ticker, 'name', f.name, 'sector', f.sector,
            'as_of', pp.as_of, 'close', pp.close, 'ret_1d', pp.ret_1d,
            'ret_5d', pp.ret_5d, 'volume', pp.volume, 'avg_vol_20d', pp.avg_vol_20d,
            'vol_ratio', pp.vol_ratio, 'dollar_volume', round(pp.close * pp.volume))
    FROM price_perf pp
    LEFT JOIN fundamentals f ON f.ticker = pp.ticker
    WHERE pp.ret_1d IS NOT NULL AND pp.ret_1d < 0
      AND pp.close       >= cfg('min_price')
      AND pp.avg_vol_20d >= cfg('min_avg_vol_20d')
      AND coalesce(f.is_delisted, false) = false
    ORDER BY pp.ret_1d ASC, pp.volume DESC
    LIMIT cfg('top_n_movers')::int;

    -- ================= sector_heatmap =================
    -- One row per sector: the cap-weighted 1-day return across that sector's
    -- constituents in the same liquid universe as the movers, plus breadth
    -- (advancers / decliners). Cap-weighting mirrors how a real sector tile is
    -- built and keeps a single large name from being drowned out by small ones.
    -- Ranked strongest-to-weakest so the renderer lays the grid out in order.
    INSERT INTO newsletter_findings (issue_date, section, rank, ticker, headline, facts)
    SELECT p_issue_date, 'sector_heatmap',
        row_number() OVER (ORDER BY s.wret DESC NULLS LAST),
        NULL,
        format('%s: %s%% cap-weighted across %s names (%s up / %s down).',
               s.sector,
               CASE WHEN s.wret >= 0 THEN '+' || s.wret ELSE s.wret::text END,
               s.n, s.n_adv, s.n_dec),
        jsonb_build_object(
            'sector', s.sector, 'weighted_ret_1d', s.wret, 'avg_ret_1d', s.aret,
            'n', s.n, 'n_advancing', s.n_adv, 'n_declining', s.n_dec,
            'total_market_cap', s.mktcap)
    FROM (
        SELECT f.sector,
               round(sum(pp.ret_1d * f.market_cap) / nullif(sum(f.market_cap), 0), 2) AS wret,
               round(avg(pp.ret_1d), 2) AS aret,
               count(*) AS n,
               count(*) FILTER (WHERE pp.ret_1d > 0) AS n_adv,
               count(*) FILTER (WHERE pp.ret_1d < 0) AS n_dec,
               sum(f.market_cap) AS mktcap
        FROM price_perf pp
        JOIN fundamentals f ON f.ticker = pp.ticker
        WHERE pp.ret_1d IS NOT NULL
          AND f.sector IS NOT NULL
          AND f.market_cap IS NOT NULL AND f.market_cap > 0
          AND pp.close       >= cfg('min_price')
          AND pp.avg_vol_20d >= cfg('min_avg_vol_20d')
          AND coalesce(f.is_delisted, false) = false
        GROUP BY f.sector
    ) s;

    SELECT count(*) INTO v_count FROM newsletter_findings WHERE issue_date = p_issue_date;
    RETURN v_count;
END;
$$;


CREATE OR REPLACE FUNCTION newsletter_payload(p_issue_date date DEFAULT CURRENT_DATE)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_object_agg(section, items)
    FROM (
        SELECT section,
               jsonb_agg(jsonb_build_object('rank', rank, 'headline', headline, 'facts', facts)
                         ORDER BY rank) AS items
        FROM newsletter_findings WHERE issue_date = p_issue_date GROUP BY section
    ) s;
$$;
