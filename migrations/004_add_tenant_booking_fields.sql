-- Add booking amount and pending deposit fields to tenants table
ALTER TABLE rental_core.tenants 
ADD COLUMN IF NOT EXISTS booking_amount_paid NUMERIC DEFAULT 0.00 CHECK (booking_amount_paid >= 0),
ADD COLUMN IF NOT EXISTS pending_deposit_due_date DATE;

-- Update existing records
UPDATE rental_core.tenants SET booking_amount_paid = 0 WHERE booking_amount_paid IS NULL;
