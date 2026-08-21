-- Phase 8: Per-billing-month next-due-date reminders.
--
-- tenant_reminders: one row per tenant commitment, scoped to a single
-- billing_month (YYYY-MM). Multiple rows are allowed for the same
-- (tenant_id, billing_month) because a tenant may first give one date and
-- then reschedule; the newest ACTIVE row is the live reminder and older ones
-- are flipped to SUPERSEDED. A reminder is tied to exactly one billing month
-- so a date set for month A never bleeds into month B.
--
-- Auto-send rules (run daily by send_due_reminders.py):
--   * reminder_date = today+1  and d1_sent_at IS NULL  -> send "day before"
--   * reminder_date = today    and due_sent_at IS NULL -> send "on due date"
--   * a NOTIFY message ("Your New Next Due Date is : ...") is queued whenever
--     a reminder is set/updated and sent by the same sender.
--
-- reminder_send_log: audit trail of every reminder-related send.
BEGIN;

CREATE TABLE IF NOT EXISTS rental_core.tenant_reminders (
    id              serial PRIMARY KEY,
    tenant_id       integer      NOT NULL REFERENCES rental_core.tenants(id) ON DELETE CASCADE,
    billing_month   varchar(10)  NOT NULL,
    reminder_date   date         NOT NULL,
    amount_due      numeric(12,2),
    note            text         NOT NULL DEFAULT '',
    status          varchar(20)  NOT NULL DEFAULT 'ACTIVE',
    notify_sent_at  timestamp,
    d1_sent_at      timestamp,
    due_sent_at     timestamp,
    created_at      timestamp    NOT NULL DEFAULT now(),
    updated_at      timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT tenant_reminders_billing_month_check
        CHECK (billing_month ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT tenant_reminders_status_check
        CHECK (status IN ('ACTIVE','DONE','CANCELLED','SUPERSEDED'))
);

CREATE INDEX IF NOT EXISTS idx_tenant_reminders_tenant_month
    ON rental_core.tenant_reminders (tenant_id, billing_month);
CREATE INDEX IF NOT EXISTS idx_tenant_reminders_due
    ON rental_core.tenant_reminders (reminder_date, status);

CREATE TABLE IF NOT EXISTS rental_core.reminder_send_log (
    id           serial PRIMARY KEY,
    batch_id     varchar(40)  NOT NULL,
    reminder_id  integer REFERENCES rental_core.tenant_reminders(id) ON DELETE SET NULL,
    tenant_id    integer REFERENCES rental_core.tenants(id) ON DELETE SET NULL,
    phone_number varchar(50),
    send_type    varchar(20)  NOT NULL DEFAULT 'NOTIFY',
    message      text,
    status       varchar(20)  NOT NULL DEFAULT 'QUEUED',
    error        text,
    sent_at      timestamp,
    created_at   timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT reminder_send_log_type_check
        CHECK (send_type IN ('NOTIFY','D1','DUE')),
    CONSTRAINT reminder_send_log_status_check
        CHECK (status IN ('QUEUED','SENT','FAILED','SKIPPED'))
);

CREATE INDEX IF NOT EXISTS idx_reminder_send_log_batch
    ON rental_core.reminder_send_log (batch_id);
CREATE INDEX IF NOT EXISTS idx_reminder_send_log_created
    ON rental_core.reminder_send_log (created_at);

COMMIT;