-- 008_gas_meter_units.sql — add meter reading + billing period + consumed units to gas_bill_master
-- Covers 005_gas_bill_enhanced_fields + meter units (probe-verified: MeterReadingHistory.aspx table#table1)
-- Idempotent — safe if 005 was partially applied or not applied
BEGIN;

-- Enhanced fields from 005 (if not already present)
ALTER TABLE rental_core.gas_bill_master
    ADD COLUMN IF NOT EXISTS invoice_no VARCHAR(50),
    ADD COLUMN IF NOT EXISTS billing_period VARCHAR(100),
    ADD COLUMN IF NOT EXISTS period_from DATE,
    ADD COLUMN IF NOT EXISTS period_to DATE,
    ADD COLUMN IF NOT EXISTS invoice_date DATE,
    ADD COLUMN IF NOT EXISTS invoice_due_date DATE,
    ADD COLUMN IF NOT EXISTS previous_paid_date DATE,
    ADD COLUMN IF NOT EXISTS previous_paid_amount NUMERIC DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS bp_number VARCHAR(50),
    ADD COLUMN IF NOT EXISTS customer_name VARCHAR(200),
    ADD COLUMN IF NOT EXISTS source_file VARCHAR(200),
    ADD COLUMN IF NOT EXISTS is_bimonthly BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();

-- Meter reading provenance (verified probe 2026-09-14: 500000832931 => 4594/4533 SCM, consumed 61 SCM on table#table1)
ALTER TABLE rental_core.gas_bill_master
    ADD COLUMN IF NOT EXISTS meter_reading_latest NUMERIC,
    ADD COLUMN IF NOT EXISTS meter_reading_previous NUMERIC,
    ADD COLUMN IF NOT EXISTS reading_date_latest DATE,
    ADD COLUMN IF NOT EXISTS reading_date_previous DATE,
    ADD COLUMN IF NOT EXISTS reading_reason_latest VARCHAR(100),
    ADD COLUMN IF NOT EXISTS consumed_units NUMERIC,
    ADD COLUMN IF NOT EXISTS meter_source VARCHAR(50) DEFAULT 'MeterReadingHistory';

-- Unique for bimonthly period when enhanced cols present (keep basic unique_gas_bill_month for execution_month fallback)
CREATE UNIQUE INDEX IF NOT EXISTS idx_gas_bill_customer_period
    ON rental_core.gas_bill_master (customer_number, period_from, period_to)
    WHERE period_from IS NOT NULL AND period_to IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_gas_bill_invoice_no ON rental_core.gas_bill_master (invoice_no);
CREATE INDEX IF NOT EXISTS idx_gas_bill_bp_number ON rental_core.gas_bill_master (bp_number);
CREATE INDEX IF NOT EXISTS idx_gas_bill_consumed_units ON rental_core.gas_bill_master (consumed_units) WHERE consumed_units IS NOT NULL;

COMMIT;
