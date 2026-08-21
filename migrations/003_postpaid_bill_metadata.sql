-- Phase 1.4: postpaid bill metadata for torrent_automation_engine.
-- Bills fetched in month M are the PREVIOUS month's consumption (postpaid).
-- The get_amount response also carries the last-generated bill's metadata
-- (preparation date, amount, billed units). We store these for display only;
-- they must NEVER be used in ledger calculations.
BEGIN;

ALTER TABLE rental_core.torrent_bill_master
    ADD COLUMN IF NOT EXISTS last_bill_date timestamp without time zone,
    ADD COLUMN IF NOT EXISTS last_bill_amt numeric(10,2),
    ADD COLUMN IF NOT EXISTS last_billed_units numeric(12,2);

COMMENT ON COLUMN rental_core.torrent_bill_master.last_bill_amt IS
    'Last generated bill amount (torrent). DISPLAY ONLY - never used in calculations.';

COMMIT;
