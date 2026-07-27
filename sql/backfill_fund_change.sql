-- =====================================================================
-- backfill_fund_change.sql
--
-- Repairs change_shares / change_pct on existing fund_holders rows using the
-- raw Holders block already stored in fundamentals.holders (jsonb). NO API
-- calls -- the values are already in your database.
--
-- Root cause: ingest.py -> _ingest_holders wrote fund rows WITHOUT change_shares
-- / change_pct (neither branch mapped EODHD's per-holder `change` / `change_p`).
-- The institutional side only had the column because 00_fix_holders.sql
-- backfilled it out-of-band -- and that script only ever touched
-- institutional_holders, so fund_flow's `WHERE change_shares IS NOT NULL`
-- filtered out all 96,500 fund rows and fund_flow_ticker returned 0.
--
-- The corrected ingest.py now maps change/change_p at ingest for BOTH branches,
-- so this backfill is a one-time repair of history. (00_fix_holders.sql becomes
-- redundant once the corrected ingest has run a full cycle.)
--
-- Match key is the fund_holders PK (ticker, holder_name, report_date) -- the
-- fields the bug left intact. Only rows still missing change_shares are touched.
--
-- Target: PostgreSQL 16. Review the verification query, then COMMIT.
-- =====================================================================

BEGIN;

-- Optional safety snapshot:
-- CREATE TABLE fund_holders_bak AS TABLE fund_holders;

WITH raw AS (
    SELECT
        f.ticker,
        e.value->>'name'                          AS holder_name,
        (e.value->>'date')::date                  AS report_date,
        (e.value->>'change')::numeric             AS change_shares,
        (e.value->>'change_p')::numeric           AS change_pct
    FROM fundamentals f
    CROSS JOIN LATERAL jsonb_each(f.holders->'Funds') AS e(key, value)
    WHERE f.holders ? 'Funds'
      AND jsonb_typeof(f.holders->'Funds') = 'object'
      AND (e.value->>'name') IS NOT NULL
      AND (e.value->>'date') IS NOT NULL
      AND (e.value->>'change') IS NOT NULL
)
UPDATE fund_holders h
SET change_shares = r.change_shares,
    change_pct    = r.change_pct
FROM raw r
WHERE h.ticker      = r.ticker
  AND h.holder_name = r.holder_name
  AND h.report_date = r.report_date
  AND h.change_shares IS NULL;

-- Verify before committing. Expect with_change to jump from 0 toward the total.
--   SELECT count(*) AS total,
--          count(change_shares) AS with_change,
--          count(*) FILTER (WHERE change_shares IS NULL) AS still_null
--   FROM fund_holders;
--
-- Then confirm the rollup wakes up (was 0):
--   REFRESH MATERIALIZED VIEW CONCURRENTLY fund_flow;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY fund_flow_ticker;
--   SELECT count(*) FROM fund_flow_ticker;

COMMIT;
