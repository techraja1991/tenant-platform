-- Phase 7: WhatsApp reminder message templates + tenant mappings + send log.
--
-- message_templates: master store of reminder templates. One active template
-- may be marked is_default='Y' (used for tenants without an explicit mapping);
-- templates can also be mapped per-tenant for individual overrides.
-- Body/note/signature support {placeholders} which the renderer fills from the
-- tenant's ledger hisab (see gui_api.py render_whatsapp_message).
BEGIN;

CREATE TABLE IF NOT EXISTS rental_core.message_templates (
    id            serial PRIMARY KEY,
    template_name varchar(100) NOT NULL,
    template_type varchar(30)  NOT NULL DEFAULT 'REMINDER',
    body          text         NOT NULL,
    note          text         NOT NULL DEFAULT '',
    signature     text         NOT NULL DEFAULT '',
    is_active     bpchar(1)    NOT NULL DEFAULT 'Y',
    is_default    bpchar(1)    NOT NULL DEFAULT 'N',
    created_at    timestamp    NOT NULL DEFAULT now(),
    updated_at    timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT message_templates_is_active_check  CHECK (is_active  IN ('Y','N')),
    CONSTRAINT message_templates_is_default_check CHECK (is_default IN ('Y','N'))
);

CREATE TABLE IF NOT EXISTS rental_core.tenant_message_templates (
    tenant_id   integer PRIMARY KEY REFERENCES rental_core.tenants(id) ON DELETE CASCADE,
    template_id integer NOT NULL REFERENCES rental_core.message_templates(id) ON DELETE CASCADE,
    created_at  timestamp NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rental_core.whatsapp_send_log (
    id            serial PRIMARY KEY,
    batch_id      varchar(40)  NOT NULL,
    tenant_id     integer REFERENCES rental_core.tenants(id) ON DELETE SET NULL,
    phone_number  varchar(50),
    template_id   integer,
    template_name varchar(100),
    billing_month varchar(20),
    message       text,
    status        varchar(20)  NOT NULL DEFAULT 'QUEUED',
    error         text,
    sent_at       timestamp,
    created_at    timestamp    NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_send_log_status_check CHECK (status IN ('QUEUED','SENT','FAILED','SKIPPED'))
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_send_log_batch   ON rental_core.whatsapp_send_log (batch_id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_send_log_created ON rental_core.whatsapp_send_log (created_at);

-- Enforce single default: any template set as default clears the others.
CREATE OR REPLACE FUNCTION rental_core.ensure_single_default_template()
RETURNS trigger AS $$
BEGIN
    IF NEW.is_default = 'Y' THEN
        UPDATE rental_core.message_templates
           SET is_default = 'N'
         WHERE is_default = 'Y' AND id <> NEW.id;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_message_templates_default ON rental_core.message_templates;
CREATE TRIGGER trg_message_templates_default
    BEFORE INSERT OR UPDATE ON rental_core.message_templates
    FOR EACH ROW EXECUTE FUNCTION rental_core.ensure_single_default_template();

COMMIT;
