-- Phase 0.6-0.9 migration: indexes, dedup unique, created_at, CHECK constraints
BEGIN;

-- ============ 0.7 Remove duplicate unique index on utility_metrics.service_number ============
ALTER TABLE rental_core.utility_metrics DROP CONSTRAINT utility_metrics_service_number_key;

-- ============ 0.8 Add created_at audit column to monthly_ledgers ============
ALTER TABLE rental_core.monthly_ledgers ADD COLUMN created_at timestamp without time zone DEFAULT now();

-- ============ 0.6 Indexes on hot FK / filter columns ============
CREATE INDEX idx_tenants_room_id ON rental_core.tenants(room_id);
CREATE INDEX idx_tenants_is_active ON rental_core.tenants(is_active);
CREATE INDEX idx_rooms_master_id ON rental_core.rooms(master_id);
CREATE INDEX idx_monthly_ledgers_billing_month ON rental_core.monthly_ledgers(billing_month);
CREATE INDEX idx_monthly_ledgers_room_id ON rental_core.monthly_ledgers(room_id);
CREATE INDEX idx_tenant_payments_billing_month ON rental_core.tenant_payments(billing_month);
CREATE INDEX idx_tenant_payments_tenant_id ON rental_core.tenant_payments(tenant_id);
CREATE INDEX idx_tenant_payments_room_id ON rental_core.tenant_payments(room_id);
CREATE INDEX idx_pme_execution_month ON rental_core.property_maintenance_expenses(execution_month);
CREATE INDEX idx_pme_room_id ON rental_core.property_maintenance_expenses(room_id);
CREATE INDEX idx_gas_bill_master_execution_month ON rental_core.gas_bill_master(execution_month);
CREATE INDEX idx_gas_metrics_room_id ON rental_core.gas_metrics(room_id);
CREATE INDEX idx_torrent_bill_master_execution_month ON rental_core.torrent_bill_master(execution_month);
CREATE INDEX idx_tenant_ledgers_room_id ON rental_core.tenant_ledgers(room_id);
CREATE INDEX idx_tenant_ledgers_execution_month ON rental_core.tenant_ledgers(execution_month);
CREATE INDEX idx_utility_metrics_room_id ON rental_core.utility_metrics(room_id);
CREATE INDEX idx_utility_metrics_account_id ON rental_core.utility_metrics(account_id);

-- ============ 0.9 CHECK constraints ============
-- Normalize legacy month format (Jun-26 -> 2026-06) before constraining
UPDATE rental_core.monthly_ledgers SET billing_month = '2026-06' WHERE billing_month = 'Jun-26';

-- monthly_ledgers
ALTER TABLE rental_core.monthly_ledgers
    ADD CONSTRAINT ml_billing_month_format_check
    CHECK (billing_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
ALTER TABLE rental_core.monthly_ledgers
    ADD CONSTRAINT ml_amounts_non_negative_check
    CHECK (prev_baki >= 0 AND prev_jama >= 0 AND bhadu_applied >= 0 AND light_bill >= 0
           AND gas_bill >= 0 AND maintenance_applied >= 0 AND jama >= 0 AND common_misc_expenses >= 0);

-- tenant_payments
ALTER TABLE rental_core.tenant_payments
    ADD CONSTRAINT tp_billing_month_format_check
    CHECK (billing_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
ALTER TABLE rental_core.tenant_payments
    ADD CONSTRAINT tp_amount_positive_check
    CHECK (amount > 0);

-- tenant_ledgers
ALTER TABLE rental_core.tenant_ledgers
    ADD CONSTRAINT tl_execution_month_format_check
    CHECK (execution_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');

-- gas_bill_master
ALTER TABLE rental_core.gas_bill_master
    ADD CONSTRAINT gbm_execution_month_format_check
    CHECK (execution_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
ALTER TABLE rental_core.gas_bill_master
    ADD CONSTRAINT gbm_amount_non_negative_check
    CHECK (amount >= 0);

-- torrent_bill_master (nullable columns)
ALTER TABLE rental_core.torrent_bill_master
    ADD CONSTRAINT tbm_execution_month_format_check
    CHECK (execution_month IS NULL OR execution_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
ALTER TABLE rental_core.torrent_bill_master
    ADD CONSTRAINT tbm_amount_non_negative_check
    CHECK (amount IS NULL OR amount >= 0);

-- property_maintenance_expenses
ALTER TABLE rental_core.property_maintenance_expenses
    ADD CONSTRAINT pme_execution_month_format_check
    CHECK (execution_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
ALTER TABLE rental_core.property_maintenance_expenses
    ADD CONSTRAINT pme_amount_non_negative_check
    CHECK (amount >= 0);

-- tenants
ALTER TABLE rental_core.tenants
    ADD CONSTRAINT tenants_is_active_check
    CHECK (is_active IS NULL OR is_active IN ('Y', 'N'));
ALTER TABLE rental_core.tenants
    ADD CONSTRAINT tenants_amounts_non_negative_check
    CHECK (base_rent_applied >= 0 AND maintenance_applied >= 0 AND security_deposit_applied >= 0);

-- property_masters
ALTER TABLE rental_core.property_masters
    ADD CONSTRAINT pm_amounts_non_negative_check
    CHECK (default_base_rent >= 0 AND default_maintenance_fee >= 0 AND default_security_deposit >= 0 AND default_common_utility >= 0);

COMMIT;
