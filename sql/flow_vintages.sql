-- =====================================================================
-- flow_vintages.sql
--
-- The vintage-banking layer. The holder/fund rollups (inst_flow_ticker,
-- fund_flow_ticker) are rebuilt from a CURRENT snapshot every refresh, so on
-- their own they only ever answer "what is true now". They cannot answer "what
-- did net_change_pct say three months ago", because that reading was overwritten
-- -- which is exactly why institutional_flow is prospective-only.
--
-- This layer fixes that the only honest way it can be fixed: by APPENDING a
-- dated copy of each rollup every time we refresh, so vintages accumulate going
-- forward. It cannot manufacture history it never observed -- a backtest can
-- only reach back to the first banked date -- but from that date on, the signal
-- becomes point-in-time queryable. This is the same "forward-accumulating"
-- pattern 01_metrics_views.sql notes for the analyst ratings, generalized to
-- holders and funds.
--
-- KNOWABILITY STAMP. Each vintage carries banked_at (the date we snapshotted
-- it). A point-in-time reader gates on banked_at <= as_of: "the latest reading
-- our system had actually stored by as_of". banked_at, not the holders'
-- observed_at or the 13F filing date, is the honest answer to "what did we know
-- then", because it is when the row entered our queryable store.
--
-- Tables are cloned from the rollups with CREATE TABLE AS ... WITH NO DATA, so
-- their columns and types track the source views exactly -- no hand-typed schema
-- to drift. banked_at is prepended, so `SELECT CURRENT_DATE, t.*` lines up.
--
-- Run order: after 01_metrics_views.sql. Then call bank_flow_vintages() (or
-- refresh_and_bank()) on your refresh schedule. Target: PostgreSQL 16.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Vintage tables. Column shape is inherited from the rollups; banked_at
-- is prepended as the knowability stamp and part of the key.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inst_flow_vintage AS
    SELECT CURRENT_DATE AS banked_at, t.* FROM inst_flow_ticker t WITH NO DATA;

CREATE TABLE IF NOT EXISTS fund_flow_vintage AS
    SELECT CURRENT_DATE AS banked_at, t.* FROM fund_flow_ticker t WITH NO DATA;

-- One vintage per ticker per bank date. The PK also serves the
-- latest-vintage-as-of lookup (ticker, banked_at DESC).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'inst_flow_vintage_pk') THEN
        ALTER TABLE inst_flow_vintage ADD CONSTRAINT inst_flow_vintage_pk
            PRIMARY KEY (ticker, banked_at);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fund_flow_vintage_pk') THEN
        ALTER TABLE fund_flow_vintage ADD CONSTRAINT fund_flow_vintage_pk
            PRIMARY KEY (ticker, banked_at);
    END IF;
END$$;


-- ---------------------------------------------------------------------
-- bank_flow_vintages() : append today's rollup, but only for tickers whose
-- reading actually CHANGED since their last banked vintage. Content-dedup
-- keeps the tables from growing by ~5,300 rows every day when the underlying
-- 13F/fund data is stable for the quarter; a name is re-banked only when its
-- flow, filing date, holder count, or concentration moved.
--
-- Returns the number of inst and fund vintages written this run.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bank_flow_vintages()
RETURNS TABLE(inst_banked integer, fund_banked integer)
LANGUAGE plpgsql AS $$
DECLARE
    v_inst integer;
    v_fund integer;
BEGIN
    INSERT INTO inst_flow_vintage
    SELECT CURRENT_DATE, t.*
    FROM inst_flow_ticker t
    LEFT JOIN LATERAL (
        SELECT * FROM inst_flow_vintage v
        WHERE v.ticker = t.ticker
        ORDER BY v.banked_at DESC
        LIMIT 1
    ) prev ON true
    WHERE prev.ticker IS NULL
       OR prev.net_change_pct            IS DISTINCT FROM t.net_change_pct
       OR prev.latest_filing             IS DISTINCT FROM t.latest_filing
       OR prev.top_n_holders             IS DISTINCT FROM t.top_n_holders
       OR prev.largest_holder_pct        IS DISTINCT FROM t.largest_holder_pct
       OR prev.max_holder_conviction_pct IS DISTINCT FROM t.max_holder_conviction_pct
    ON CONFLICT (ticker, banked_at) DO NOTHING;
    GET DIAGNOSTICS v_inst = ROW_COUNT;

    INSERT INTO fund_flow_vintage
    SELECT CURRENT_DATE, t.*
    FROM fund_flow_ticker t
    LEFT JOIN LATERAL (
        SELECT * FROM fund_flow_vintage v
        WHERE v.ticker = t.ticker
        ORDER BY v.banked_at DESC
        LIMIT 1
    ) prev ON true
    WHERE prev.ticker IS NULL
       OR prev.net_change_pct IS DISTINCT FROM t.net_change_pct
       OR prev.latest_filing  IS DISTINCT FROM t.latest_filing
       OR prev.top_n_funds    IS DISTINCT FROM t.top_n_funds
    ON CONFLICT (ticker, banked_at) DO NOTHING;
    GET DIAGNOSTICS v_fund = ROW_COUNT;

    RETURN QUERY SELECT v_inst, v_fund;
END;
$$;


-- ---------------------------------------------------------------------
-- Convenience: refresh the rollups then bank them, in one call, for the cron.
-- Banking reads the rollups, so it must run AFTER refresh_metrics().
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION refresh_and_bank() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM refresh_metrics();
    PERFORM bank_flow_vintages();
END;
$$;

-- Seed the first vintage now (and eyeball the counts):
--   SELECT * FROM bank_flow_vintages();
-- Then on your schedule, replace the refresh_metrics() call with
-- refresh_and_bank(), or add bank_flow_vintages() right after it.

-- Intra-day note: a second bank on the same calendar day whose content changed
-- again is dropped by the (ticker, banked_at) key -- one vintage per day. Fine
-- for holder data that updates at most daily; worth knowing if you ever bank
-- more than once a day.
