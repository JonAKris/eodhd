-- =====================================================================
-- migration_insider.sql
--
-- Adds the two columns the dedicated insider endpoint provides but the old
-- fundamentals-block source did not: the SEC filing date and the owner title.
-- Idempotent; run before the updated ingest.py ingest_insider().
--
--   report_date : SEC Form 4 filing/acceptance date -- the knowability date.
--                 This is what lets the insider strategy's as-of gate become
--                 exact instead of the proxy:Nd lag.
--   owner_title : e.g. 'CEO', 'Director', 'General Counsel'. ownerRelationship
--                 is frequently null in the feed; ownerTitle carries the role.
-- =====================================================================

ALTER TABLE insider_transactions ADD COLUMN IF NOT EXISTS report_date date;
ALTER TABLE insider_transactions ADD COLUMN IF NOT EXISTS owner_title text;

COMMENT ON COLUMN insider_transactions.report_date IS
    'SEC Form 4 filing/acceptance date (knowability date); from the dedicated insider endpoint';
COMMENT ON COLUMN insider_transactions.owner_title IS
    'Reporting owner title/role, e.g. CEO/Director; from the dedicated insider endpoint';

CREATE INDEX IF NOT EXISTS insider_transactions_report_date_idx
    ON insider_transactions (report_date);
