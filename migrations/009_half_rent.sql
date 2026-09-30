-- 009_half_rent.sql — Add default half rent for prorated mid-month leases
-- Spec: lease start after 15th => half rent for that month, lease end 1st-15th => half rent
-- Half rent is not exact half: 3300->1700, 6500->3400, 7000->3700, etc. Stored per property unit.

BEGIN;

-- Property master default half rent (nullable, if set used for ledgers when tenant starts/ends mid-month)
ALTER TABLE rental_core.property_masters
    ADD COLUMN IF NOT EXISTS default_half_rent numeric(10,2) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS updated_at timestamp DEFAULT now();

-- Rooms can override property default half rent per unit (e.g., different flat sizes)
ALTER TABLE rental_core.rooms
    ADD COLUMN IF NOT EXISTS half_rent numeric(10,2) DEFAULT NULL;

-- Tenants store applied half rent (snapshot at creation, fallback to room/property)
ALTER TABLE rental_core.tenants
    ADD COLUMN IF NOT EXISTS half_rent_applied numeric(10,2) DEFAULT NULL;

-- Populate property defaults from known mappings, fallback to ceil(base/2) rounded to 100
UPDATE rental_core.property_masters SET default_half_rent = CASE
    WHEN default_base_rent = 3300 THEN 1700
    WHEN default_base_rent = 6500 THEN 3400
    WHEN default_base_rent = 7000 THEN 3700
    WHEN default_base_rent = 5000 THEN 2600
    WHEN default_base_rent = 2500 THEN 1350
    WHEN default_base_rent = 3000 THEN 1600
    WHEN default_base_rent = 6400 THEN 3300
    WHEN default_base_rent = 0 THEN 0
    ELSE CEIL(default_base_rent / 2 / 100) * 100
END
WHERE default_half_rent IS NULL;

-- For existing tenants, backfill half_rent_applied from their current base_rent using same mapping if not already set
UPDATE rental_core.tenants SET half_rent_applied = CASE
    WHEN base_rent_applied = 3300 THEN 1700
    WHEN base_rent_applied = 6500 THEN 3400
    WHEN base_rent_applied = 7000 THEN 3700
    WHEN base_rent_applied = 5000 THEN 2600
    WHEN base_rent_applied = 2500 THEN 1350
    WHEN base_rent_applied = 3000 THEN 1600
    WHEN base_rent_applied = 6400 THEN 3300
    WHEN base_rent_applied = 0 THEN 0
    ELSE CEIL(base_rent_applied / 2 / 100) * 100
END
WHERE half_rent_applied IS NULL;

COMMIT;
