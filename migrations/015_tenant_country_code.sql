-- 015_tenant_country_code.sql — Country dial code per tenant for WhatsApp links.
--
-- Phone numbers are stored as local digits (existing rows are Indian
-- 10-digit numbers). country_code ('91' default, '977' Nepal) tells link
-- builders which account a wa.me URL must open. No backfill needed:
-- default '91' matches all existing data.
BEGIN;

ALTER TABLE rental_core.tenants
    ADD COLUMN IF NOT EXISTS country_code character varying(5) DEFAULT '91';

UPDATE rental_core.tenants SET country_code = '91' WHERE country_code IS NULL;

ALTER TABLE rental_core.tenants
    DROP CONSTRAINT IF EXISTS tenants_country_code_check;

ALTER TABLE rental_core.tenants
    ADD CONSTRAINT tenants_country_code_check
    CHECK (country_code IN ('91', '977'));

COMMIT;
