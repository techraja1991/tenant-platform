-- Add enhanced fields to gas_bill_master for detailed Gujarat Gas bill tracking
ALTER TABLE rental_core.gas_bill_master
ADD COLUMN IF NOT EXISTS invoice_no VARCHAR(50),
ADD COLUMN IF NOT EXISTS billing_period VARCHAR(100),
ADD COLUMN IF NOT EXISTS period_from DATE,
ADD COLUMN IF NOT EXISTS period_to DATE,
ADD COLUMN IF NOT EXISTS invoice_due_date DATE,
ADD COLUMN IF NOT EXISTS previous_paid_date DATE,
ADD COLUMN IF NOT EXISTS previous_paid_amount NUMERIC DEFAULT 0.00,
ADD COLUMN IF NOT EXISTS bp_number VARCHAR(50),
ADD COLUMN IF NOT EXISTS customer_name VARCHAR(200),
ADD COLUMN IF NOT EXISTS source_file VARCHAR(200),
ADD COLUMN IF NOT EXISTS is_bimonthly BOOLEAN DEFAULT TRUE;

-- Add unique constraint for bimonthly billing (prevent duplicate imports)
CREATE UNIQUE INDEX IF NOT EXISTS idx_gas_bill_customer_period 
ON rental_core.gas_bill_master (customer_number, period_from, period_to)
WHERE period_from IS NOT NULL AND period_to IS NOT NULL;

-- Add index for faster lookups
CREATE INDEX IF NOT EXISTS idx_gas_bill_invoice_no ON rental_core.gas_bill_master (invoice_no);
CREATE INDEX IF NOT EXISTS idx_gas_bill_bp_number ON rental_core.gas_bill_master (bp_number);
