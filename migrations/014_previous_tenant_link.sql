-- 014_previous_tenant_link.sql — Link a new stay to the same person's previous stay.
--
-- One row per stay is the rule (never reactivate closed rows). When the same
-- person re-leases (same or different unit after a gap), the new tenant row
-- stores previous_tenant_id purely as a navigational link for a future
-- full-person-history view. Zero financial effect: ledgers, payments and
-- deposit tracking stay strictly per stay (per tenant_id).
BEGIN;

ALTER TABLE rental_core.tenants
    ADD COLUMN IF NOT EXISTS previous_tenant_id integer
    REFERENCES rental_core.tenants(id);

CREATE INDEX IF NOT EXISTS idx_tenants_previous_tenant_id
    ON rental_core.tenants (previous_tenant_id);

COMMIT;
