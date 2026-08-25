"""Per-billing-month next-due-date reminder messages.

Builds the short "next due date" messages sent to tenants:

  * NOTIFY - sent as soon as a reminder date is set/updated:
        "Your New Next Due Date is : <date>"
        "Amount to be paid : Rs. <amount>"
  * D1     - sent one day before the reminder date:
        "Reminder: Your due date is tomorrow, <date>"
  * DUE    - sent on the reminder date:
        "Reminder: Your due date is today, <date>"

Each reminder is scoped to exactly one billing_month; the amount shown is the
outstanding (new_baki) captured for that billing month at set time.
"""
from datetime import datetime, date

from db_config import get_db_connection
import whatsapp_message as wm


def _fmt_amount(v):
    v = float(v or 0.0)
    if v == int(v):
        return f"{int(v)}"
    return f"{v:.2f}"


def _fmt_date(d):
    if d is None:
        return ""
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return str(d)[:10]


def _fmt_month(ym):
    try:
        return datetime.strptime(ym, "%Y-%m").strftime("%b-%y")
    except Exception:
        return ym


def _tenant(cur, tenant_id):
    cur.execute("""
        SELECT t.tenant_name, t.phone_number, r.display_address
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE t.id = %s
    """, (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"Tenant {tenant_id} not found")
    return row


def reminder_payload(cur, reminder, rem_type='rent', paid_amount=None, paid_date=None):
    """Build NOTIFY/D1/DUE messages for one tenant_reminders row.
    Type-aware: rent vs security deposit. When paid_amount is provided,
    includes a payment acknowledgment line."""
    tenant = _tenant(cur, reminder['tenant_id'])
    name = tenant['tenant_name']
    address = tenant['display_address'] or ""
    amount = _fmt_amount(reminder.get('amount_due'))
    rdate = _fmt_date(reminder['reminder_date'])
    month = _fmt_month(reminder['billing_month'])
    header = f"{name}"
    if address:
        header += f" ({address})"

    type_label = "Security Deposit" if rem_type == 'deposit' else "Rent"

    lines = {
        "header": header,
        "month": f"Billing Month : {month}",
        "due": f"Your New Next Due Date is : {rdate}",
        "amount": f"Amount to be paid : Rs. {amount}",
    }

    # Payment acknowledgment (when set from Record Payment flow)
    if paid_amount is not None and paid_amount > 0:
        pd = _fmt_date(paid_date) if paid_date else ""
        lines["payment"] = f"Your amount for {type_label.lower()} received today : {pd} — Rs. {float(paid_amount):,.0f}"

    # Type-aware due line
    if rem_type == 'deposit':
        lines["due"] = f"Next pending due date for security deposit : {rdate} — Rs. {amount}"
        if "amount" in lines:
            lines["amount"] = f"Pending deposit to be paid : Rs. {amount}"
    else:
        lines["due"] = f"Your New Next Due Date is : {rdate} — Rs. {amount}"
        if "payment" in lines:
            lines["amount"] = f"Remaining rent to be paid : Rs. {amount}"

    # Build message order
    msg_parts = [lines["header"]]
    if "payment" in lines:
        msg_parts.append(lines["payment"])
    msg_parts.append(lines["due"])
    if lines["amount"] not in msg_parts:
        msg_parts.append(lines["amount"])

    hisab = None
    try:
        hisab, _, _ = wm.render_message(cur, reminder['tenant_id'], reminder['billing_month'])
    except Exception:
        hisab = None
    return {
        "tenant_id": reminder['tenant_id'],
        "tenant_name": name,
        "phone_number": tenant['phone_number'],
        "display_address": address,
        "billing_month": reminder['billing_month'],
        "reminder_date": str(reminder['reminder_date'])[:10],
        "amount_due": reminder.get('amount_due'),
        "notify": "\n".join(msg_parts),
        "d1": "\n".join([lines["header"],
                         f"Reminder: Your due date is TOMORROW, {rdate}.",
                         lines["month"], lines["amount"]]),
        "due": "\n".join([lines["header"],
                          f"Reminder: Your due date is TODAY, {rdate}.",
                          lines["month"], lines["amount"]]),
        "hisab": hisab,
        "wa_link": wm.wa_link(tenant['phone_number'],
                              "\n".join([lines["header"], lines["due"], lines["amount"]])),
    }


def current_outstanding(cur, tenant_id, billing_month):
    """Outstanding (new_baki) for the tenant in `billing_month`."""
    cur.execute("""
        SELECT new_baki FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month = %s
    """, (tenant_id, billing_month))
    row = cur.fetchone()
    return row['new_baki'] if row else None


def get_setting(key, default=None):
    """Read one app_settings value."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM rental_core.app_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
        return row['value'] if row else default
    finally:
        cur.close()
        conn.close()


def set_setting(key, value):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.app_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (key, value))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def send_time_setting():
    """Master auto-send time as 'HH:MM' (default 10:00)."""
    return get_setting("reminder_send_time", "10:00") or "10:00"


def sends_enabled():
    return (get_setting("reminder_send_enabled", "Y") or "Y").upper() == "Y"


def month_reminders(year, month):
    """All reminders whose DUE DATE falls in the given calendar month,
    newest ACTIVE first + history. Filtered by reminder_date, not billing_month."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, t.tenant_name, r2.display_address,
               CASE WHEN r.note ILIKE '%%deposit%%' THEN 'deposit' ELSE 'rent' END AS rem_type
        FROM rental_core.tenant_reminders r
        JOIN rental_core.tenants t ON r.tenant_id = t.id
        LEFT JOIN rental_core.rooms r2 ON t.room_id = r2.id
        WHERE EXTRACT(YEAR FROM r.reminder_date) = %s
          AND EXTRACT(MONTH FROM r.reminder_date) = %s
        ORDER BY r.status = 'ACTIVE' DESC, r.reminder_date ASC, r.id DESC
    """, (year, month))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows