-- 013_payment_mode_adjustment.sql — Allow 'ADJUSTMENT' payment mode.
--
-- DEPOSIT_UTILIZED settlement rows are accounting entries (deposit received
-- earlier, applied now) — not cash movements. They must not carry a cash
-- mode (CASH/UPI/...). 'ADJUSTMENT' marks them distinctly and is set only
-- server-side; user-facing inputs stay cash-only.
BEGIN;

ALTER TABLE rental_core.tenant_payments
    DROP CONSTRAINT IF EXISTS tenant_payments_payment_mode_check;

ALTER TABLE rental_core.tenant_payments
    ADD CONSTRAINT tenant_payments_payment_mode_check
    CHECK (payment_mode IN ('CASH', 'UPI', 'BANK_TRANSFER', 'CARD', 'CHEQUE', 'ADJUSTMENT'));

COMMIT;
