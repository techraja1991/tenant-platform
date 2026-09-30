-- 011_settled_estimate_flag.sql — Distinguish settlements closed on estimated
-- (uncompiled-month) utility bills from those closed on actual bills.
--
-- settled_estimate = TRUE means: the full settlement for this closing month was
-- recorded while light/gas were estimated (bills not yet arrived). When the
-- actual bills later arrive via month compile, the flag promotes to FALSE
-- (plain Settled) if actuals match the estimates, else it stays so the UI can
-- show "Settled (Est.)" plus the estimate-vs-actual variance.
BEGIN;

ALTER TABLE rental_core.monthly_ledgers
    ADD COLUMN IF NOT EXISTS settled_estimate boolean NOT NULL DEFAULT FALSE;

COMMIT;
