-- 010_deposit_settlement_flag.sql — Track deposit settlement to prevent reuse
BEGIN;

-- Add SETTLEMENT and REFUND payment types to tenant_payments
ALTER TABLE rental_core.tenant_payments
    DROP CONSTRAINT IF EXISTS tenant_payments_payment_type_check;

ALTER TABLE rental_core.tenant_payments
    ADD CONSTRAINT tenant_payments_payment_type_check
    CHECK (payment_type IN ('RENT', 'DEPOSIT', 'SETTLEMENT', 'REFUND'));

ALTER TABLE rental_core.tenants
    ADD COLUMN IF NOT EXISTS deposit_settled boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS deposit_settled_amount numeric(10,2) DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS deposit_settled_date date,
    ADD COLUMN IF NOT EXISTS settlement_history jsonb DEFAULT '[]'::jsonb;

-- For existing tenants where settlement already recorded (has SETTLEMENT/REFUND payments), mark as settled if they have been used
-- This will be handled via application logic, not DB update here

COMMIT;
