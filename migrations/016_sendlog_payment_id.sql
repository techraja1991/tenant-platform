-- 016_sendlog_payment_id.sql — Link queued sends to their payment for idempotency.
--
-- Payment-accepted notifications (no due date) are tied to a payment, not a
-- reminder row (reminder_id stays NULL for them). payment_id lets the
-- accept-notify endpoint skip re-queueing when re-saving the same payment.
BEGIN;

ALTER TABLE rental_core.reminder_send_log
    ADD COLUMN IF NOT EXISTS payment_id integer;

CREATE INDEX IF NOT EXISTS idx_reminder_send_log_payment_id
    ON rental_core.reminder_send_log (payment_id);

COMMIT;
