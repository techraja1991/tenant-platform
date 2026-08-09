-- Phase 1.3: support splitting one payment across multiple billing months.
-- Adds batch_id to tenant_payments so a single payment (one cheque/UPI/cash
-- received in one go) can fan out to several (billing_month, amount) rows that
-- share the same batch_id. Deleting any row of a batch deletes the whole batch.
BEGIN;

ALTER TABLE rental_core.tenant_payments
    ADD COLUMN IF NOT EXISTS batch_id integer;

-- Existing single-month payments become their own batch.
UPDATE rental_core.tenant_payments
SET batch_id = id
WHERE batch_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_payments_batch_id
    ON rental_core.tenant_payments(batch_id);

COMMIT;
