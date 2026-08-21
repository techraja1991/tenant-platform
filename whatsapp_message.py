"""Shared WhatsApp message rendering for the tenant platform.

Used by both gui_api.py (manual wa.me links) and send_whatsapp_reminders.py
(automated sender). Templates live in rental_core.message_templates with
per-tenant overrides in rental_core.tenant_message_templates.

Placeholders supported in template body/note/signature:
  {name}        tenant name
  {address}     room display address (e.g. "56 / 1")
  {month}       billing month label, e.g. "Jul-26"
  {prev_month}  previous month label, e.g. "Jun-26"
  {prev_baki}   carried-forward debt
  {prev_jama}   carried-forward credit
  {bhadu}       base rent applied
  {light_bill}  torrent/light bill
  {gas_bill}    gas bill
  {maintenance} maintenance applied
  {misc}        common misc expenses share
  {month_total} month total
  {gross_baki}  prev - prev_jama + month_total (fixed gross)
  {jama}        payment received
  {jama_date}   last payment date (DD-MM-YYYY or '0')
  {new_baki}    current outstanding balance
"""
import re
from datetime import datetime
from urllib.parse import quote

from db_config import get_db_connection


def _fmt_amount(v):
    v = float(v or 0.0)
    if v == int(v):
        return f"{int(v)}"
    return f"{v:.2f}"


def _fmt_month(ym):
    try:
        return datetime.strptime(ym, "%Y-%m").strftime("%b-%y")
    except Exception:
        return ym


def _prev_month(ym):
    try:
        dt = datetime.strptime(ym, "%Y-%m")
        y, m = (dt.year - 1, 12) if dt.month == 1 else (dt.year, dt.month - 1)
        return f"{y}-{m:02d}"
    except Exception:
        return ym


def normalize_phone(phone):
    """Normalize to international format without '+' for wa.me (e.g. 918223921823)."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("91"):
        return digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits


def wa_link(phone, message):
    return f"https://wa.me/{normalize_phone(phone)}?text={quote(message or '')}"


def _tenant_address(cur, tenant_id):
    cur.execute("""
        SELECT t.tenant_name, t.phone_number, r.display_address
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE t.id = %s
    """, (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"Tenant {tenant_id} not found")
    return {
        "tenant_name": row['tenant_name'],
        "phone_number": row['phone_number'],
        "display_address": row['display_address'],
    }


def _ledger(cur, tenant_id, billing_month):
    cur.execute("""
        SELECT prev_baki, prev_jama, bhadu_applied, light_bill, gas_bill,
               maintenance_applied, common_misc_expenses, month_total,
               gross_baki, jama, jama_date, new_baki
        FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month = %s
    """, (tenant_id, billing_month))
    return cur.fetchone()


def resolve_template(cur, tenant_id):
    """Pick the template for a tenant: explicit mapping first, else the active
    default template, else the single active template, else None."""
    cur.execute("""
        SELECT t.* FROM rental_core.message_templates t
        JOIN rental_core.tenant_message_templates m ON t.id = m.template_id
        WHERE m.tenant_id = %s AND t.is_active = 'Y'
        ORDER BY t.id DESC LIMIT 1
    """, (tenant_id,))
    row = cur.fetchone()
    if row is not None:
        return row
    cur.execute("""
        SELECT * FROM rental_core.message_templates
        WHERE is_active = 'Y' ORDER BY is_default DESC, id ASC LIMIT 1
    """)
    return cur.fetchone()


def _values_for(cur, tenant_id, billing_month):
    """Gather tenant + ledger data into a placeholder -> display-string map."""
    tenant = _tenant_address(cur, tenant_id)
    ledger = _ledger(cur, tenant_id, billing_month)
    col_for = {
        "prev_baki": "prev_baki",
        "prev_jama": "prev_jama",
        "bhadu": "bhadu_applied",
        "light_bill": "light_bill",
        "gas_bill": "gas_bill",
        "maintenance": "maintenance_applied",
        "misc": "common_misc_expenses",
        "month_total": "month_total",
        "gross_baki": "gross_baki",
        "jama": "jama",
        "new_baki": "new_baki",
    }
    if ledger is None:
        vals = {p: "0" for p in col_for}
        vals["jama_date"] = "0"
    else:
        vals = {}
        for p, col in col_for.items():
            vals[p] = _fmt_amount(ledger[col])
        vals["jama_date"] = str(ledger['jama_date'] or "0")

    vals.update({
        "name": tenant['tenant_name'],
        "address": tenant['display_address'] or "",
        "month": _fmt_month(billing_month),
        "prev_month": _fmt_month(_prev_month(billing_month)),
    })
    return vals


def render_message(cur, tenant_id, billing_month, template=None):
    """Render the full message body+note+signature for a tenant+month."""
    if template is None:
        template = resolve_template(cur, tenant_id)
    if template is None:
        raise LookupError(f"No active message template for tenant {tenant_id}")
    vals = _values_for(cur, tenant_id, billing_month)
    parts = [
        _subst(template['body'], vals),
        _subst(template['note'], vals),
        _subst(template['signature'], vals),
    ]
    return "\n".join(p for p in parts if p and p.strip()).strip(), template, vals


def _subst(text, vals):
    if not text:
        return ""
    for key, val in vals.items():
        text = text.replace("{" + key + "}", str(val))
    return text


def message_payload(cur, tenant_id, billing_month, template=None):
    """Convenience: return dict with phone, message, wa_link and template meta."""
    message, template, _ = render_message(cur, tenant_id, billing_month, template)
    tenant = _tenant_address(cur, tenant_id)
    return {
        "tenant_id": tenant_id,
        "phone_number": tenant['phone_number'],
        "message": message,
        "wa_link": wa_link(tenant['phone_number'], message),
        "template_id": template['id'],
        "template_name": template['template_name'],
    }
