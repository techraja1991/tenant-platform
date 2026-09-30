-- Manual overrides for ledger utility bills.
-- Allows Allocate to keep actual utility bills in *_actual columns and respect manual overrides stored in *_manual.
-- Customer View will show both actual and manual (effective) values.

BEGIN;

-- Add manual override columns (nullable, when set they override actual for calculations)
ALTER TABLE rental_core.monthly_ledgers
    ADD COLUMN IF NOT EXISTS light_bill_actual numeric(10,2),
    ADD COLUMN IF NOT EXISTS gas_bill_actual numeric(10,2),
    ADD COLUMN IF NOT EXISTS maintenance_actual numeric(10,2),
    ADD COLUMN IF NOT EXISTS common_misc_actual numeric(10,2),
    ADD COLUMN IF NOT EXISTS light_bill_manual numeric(10,2),
    ADD COLUMN IF NOT EXISTS gas_bill_manual numeric(10,2),
    ADD COLUMN IF NOT EXISTS maintenance_manual numeric(10,2),
    ADD COLUMN IF NOT EXISTS common_misc_manual numeric(10,2),
    ADD COLUMN IF NOT EXISTS manual_override boolean DEFAULT false;

-- Backfill actual columns from current values (so existing ledgers have actual preserved)
UPDATE rental_core.monthly_ledgers
SET light_bill_actual = light_bill,
    gas_bill_actual = gas_bill,
    maintenance_actual = maintenance_applied,
    common_misc_actual = common_misc_expenses
WHERE light_bill_actual IS NULL;

-- Recreate generated columns to use manual overrides when present
-- Drop old generated columns
ALTER TABLE rental_core.monthly_ledgers DROP COLUMN IF EXISTS month_total;
ALTER TABLE rental_core.monthly_ledgers DROP COLUMN IF EXISTS gross_baki;
ALTER TABLE rental_core.monthly_ledgers DROP COLUMN IF EXISTS new_baki;

-- Recreate with COALESCE(manual, actual_or_base)
-- month_total = bhadu + effective light + effective gas + effective maintenance + effective common_misc
ALTER TABLE rental_core.monthly_ledgers
    ADD COLUMN month_total numeric(10,2) GENERATED ALWAYS AS (
        bhadu_applied + COALESCE(light_bill_manual, light_bill) + COALESCE(gas_bill_manual, gas_bill) + COALESCE(maintenance_manual, maintenance_applied) + COALESCE(common_misc_manual, common_misc_expenses)
    ) STORED;

ALTER TABLE rental_core.monthly_ledgers
    ADD COLUMN gross_baki numeric(10,2) GENERATED ALWAYS AS (
        prev_baki + bhadu_applied + COALESCE(light_bill_manual, light_bill) + COALESCE(gas_bill_manual, gas_bill) + COALESCE(maintenance_manual, maintenance_applied) + COALESCE(common_misc_manual, common_misc_expenses) - prev_jama
    ) STORED;

ALTER TABLE rental_core.monthly_ledgers
    ADD COLUMN new_baki numeric(10,2) GENERATED ALWAYS AS (
        prev_baki + bhadu_applied + COALESCE(light_bill_manual, light_bill) + COALESCE(gas_bill_manual, gas_bill) + COALESCE(maintenance_manual, maintenance_applied) + COALESCE(common_misc_manual, common_misc_expenses) - prev_jama - jama
    ) STORED;

-- Indexes for manual overrides
CREATE INDEX IF NOT EXISTS idx_monthly_ledgers_manual ON rental_core.monthly_ledgers (manual_override) WHERE manual_override = true;

COMMIT;
