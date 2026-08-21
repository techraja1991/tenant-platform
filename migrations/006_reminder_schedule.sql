-- Phase 8b: Reminder master-send-time settings + richer reminder message types.
--
-- app_settings: admin-controlled key/value store. The reminder auto-send runs
-- daily at `reminder_send_time` (default 10:00 AM); `reminder_send_enabled`
-- (Y/N) turns the scheduled send on/off from the GUI.
--
-- The reminder_send_log type check is widened to allow the full template hisab
-- message which is now also dispatched on the due date itself.
BEGIN;

CREATE TABLE IF NOT EXISTS rental_core.app_settings (
    key        varchar(100) PRIMARY KEY,
    value      text NOT NULL DEFAULT '',
    updated_at timestamp NOT NULL DEFAULT now()
);

INSERT INTO rental_core.app_settings (key, value)
VALUES ('reminder_send_time', '10:00'),
       ('reminder_send_enabled', 'Y')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE rental_core.reminder_send_log
    DROP CONSTRAINT IF EXISTS reminder_send_log_type_check;
ALTER TABLE rental_core.reminder_send_log
    ADD CONSTRAINT reminder_send_log_type_check
    CHECK (send_type IN ('NOTIFY','D1','DUE','HISAB'));

COMMIT;