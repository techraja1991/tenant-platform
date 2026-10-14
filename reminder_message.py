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


# ---------------------------------------------------------------------------
# Template master: DB-backed, editable from GUI. Placeholders use {curlies}
# and are substituted with plain str.replace (unknown placeholders stay).
# ---------------------------------------------------------------------------
TEMPLATE_DEFAULTS = {
    "notify_paid_rent": (
        "{header}\n"
        "Your amount for rent received today : {paid_date} — Rs. {paid_amount}\n"
        "Next pending due date for pending rent : {due_date} — Rs. {amount}\n"
        "Pending rent to be paid : Rs. {amount}"
    ),
    "notify_paid_deposit": (
        "{header}\n"
        "Your amount for security deposit received today : {paid_date} — Rs. {paid_amount}\n"
        "Next pending due date for security deposit : {due_date} — Rs. {amount}\n"
        "Pending deposit to be paid : Rs. {amount}"
    ),
    "notify_accept_rent": (
        "{address} - {name}\n"
        "\n"
        "Rent Transaction :-\n"
        "\n"
        "Your Amount for Rent Received Today : {paid_date} : {paid_amount}/-  via {mode}\n"
        "\n"
        "Month : {month}\n"
        "\n"
        "{base} - {paid} = {diff} Rs. {result}"
    ),
    "notify_accept_deposit": (
        "{address} - {name}\n"
        "\n"
        "Security Deposit Transaction :-\n"
        "\n"
        "Your Amount for Security Deposit Received Today : {paid_date} : {paid_amount}/-  via {mode}\n"
        "\n"
        "Month : {month}\n"
        "\n"
        "{before} + {paid} = {total} Rs. TOTAL JAMA ( {baki} Rs. BAKI )"
    ),
    "notify_accept_settlement": (
        "{address} - {name}\n"
        "\n"
        "Settlement Transaction :-\n"
        "\n"
        "Your Amount for Settlement Received Today : {paid_date} : {paid_amount}/-  via {mode}\n"
        "\n"
        "Month : {month}\n"
        "\n"
        "{base} - {paid} = {diff} Rs. {result}"
        "{clear_line}"
    ),
    "notify_accept_refund": (
        "{address} - {name}\n"
        "\n"
        "Settlement Refund Transaction :-\n"
        "\n"
        "Your Refund Amount Sent Today : {paid_date} : {paid_amount}/-  via {mode}\n"
        "\n"
        "Month : {month}\n"
        "\n"
        "{before} + {paid} = {total} Rs. TOTAL REFUNDED ( {baki} Rs. BAKI )"
        "{clear_line}"
    ),
    "notify_set_rent": (
        "{header}\n"
        "Your New Next Due Date is : {due_date} — Rs. {amount}\n"
        "Amount to be paid : Rs. {amount}"
    ),
    "notify_set_deposit": (
        "{header}\n"
        "Your New Next Deposit Due Date is : {due_date} — Rs. {amount}\n"
        "Deposit to be paid : Rs. {amount}"
    ),
    "d1_rent": (
        "{header}\n"
        "Reminder: Your RENT due date is TOMORROW, {due_date}.\n"
        "Due amount : Rs. {amount}"
    ),
    "due_rent": (
        "{header}\n"
        "Reminder: Your RENT due date is TODAY, {due_date}.\n"
        "Due amount : Rs. {amount}"
    ),
    "d1_deposit": (
        "{header}\n"
        "Reminder: Your SECURITY DEPOSIT due date is TOMORROW, {due_date}.\n"
        "Due amount : Rs. {amount}"
    ),
    "due_deposit": (
        "{header}\n"
        "Reminder: Your SECURITY DEPOSIT due date is TODAY, {due_date}.\n"
        "Due amount : Rs. {amount}"
    ),
}

_tpl_cache = {}


def get_template(key):
    """Fetch a reminder template body; falls back to built-in default."""
    if key in _tpl_cache:
        return _tpl_cache[key]
    body = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT body FROM rental_core.reminder_templates WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        body = row[0] if row else None
    except Exception:
        body = None
    if not body:
        body = TEMPLATE_DEFAULTS.get(key, "")
    _tpl_cache[key] = body
    return body


def invalidate_template_cache():
    _tpl_cache.clear()


def render_template(key, **vals):
    body = get_template(key)
    out = body
    for k, v in (vals or {}).items():
        out = out.replace("{" + k + "}", str(v))
    return out


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
        SELECT t.tenant_name, t.phone_number, t.country_code,
               t.security_deposit_applied, r.display_address
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE t.id = %s
    """, (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"Tenant {tenant_id} not found")
    return row


def accept_payload(cur, payment, billing_month):
    """Build a payment-accepted (no due date) message for one tenant_payments row.

    RENT: base = that month's live new_baki + this batch's allocation to it
    (= dues immediately before this transaction); result Extra Jama / Baki /
    Full Clear. DEPOSIT: running received totals vs required deposit.
    Returns dict(message, wa_link, ...). Raises LookupError/ValueError.
    """
    tid = payment['tenant_id']
    ptype = (payment.get('payment_type') or 'RENT').upper()
    if ptype not in ('RENT', 'DEPOSIT'):
        raise ValueError("Payment-accepted messages support RENT and DEPOSIT only")
    tenant = _tenant(cur, tid)
    cc = tenant.get('country_code') or '91'
    name = tenant['tenant_name']
    address = tenant['display_address'] or ""
    mode = str(payment.get('payment_mode') or 'CASH').upper()
    paid_date = _fmt_date(payment.get('payment_date'))
    month_label = datetime.strptime(billing_month, "%Y-%m").strftime("%B-%Y")

    if ptype == 'RENT':
        cur.execute("""
            SELECT COALESCE(new_baki, 0) AS nb FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
        """, (tid, billing_month))
        lrow = cur.fetchone()
        live_nb = float(lrow['nb']) if lrow and lrow['nb'] is not None else 0.0
        if payment.get('batch_id') is not None:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS s FROM rental_core.tenant_payments
                WHERE batch_id = %s AND billing_month = %s AND payment_type = 'RENT'
            """, (payment['batch_id'], billing_month))
            alloc = float(cur.fetchone()['s'] or 0)
            if not alloc:
                alloc = float(payment['amount'] or 0)
        else:
            alloc = float(payment['amount'] or 0)
        base = live_nb + alloc
        if alloc > base + 0.005:
            result, diff = "Extra Jama", alloc - base
        elif alloc < base - 0.005:
            result, diff = "Baki", base - alloc
        else:
            result, diff = "Full Clear", 0.0
        vals = {
            "address": address, "name": name, "paid_date": paid_date,
            "paid_amount": _fmt_amount(alloc), "mode": mode, "month": month_label,
            "base": _fmt_amount(base), "paid": _fmt_amount(alloc),
            "diff": _fmt_amount(diff), "result": result,
        }
        message = render_template("notify_accept_rent", **vals)
    else:
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s FROM rental_core.tenant_payments
            WHERE tenant_id = %s AND payment_type = 'DEPOSIT'
              AND (payment_date < %s OR (payment_date = %s AND id < %s))
        """, (tid, payment['payment_date'], payment['payment_date'], payment['id']))
        before = float(cur.fetchone()['s'] or 0)
        paid = float(payment['amount'] or 0)
        total = before + paid
        required = float(tenant.get('security_deposit_applied') or 0)
        vals = {
            "address": address, "name": name, "paid_date": paid_date,
            "paid_amount": _fmt_amount(paid), "mode": mode, "month": month_label,
            "before": _fmt_amount(before), "paid": _fmt_amount(paid),
            "total": _fmt_amount(total),
            "baki": _fmt_amount(required - total),
        }
        message = render_template("notify_accept_deposit", **vals)
    return {"message": message, "wa_link": wm.wa_link(tenant['phone_number'], message, cc),
            "kind": ptype, "tenant_id": tid, "phone_number": tenant['phone_number']}


def settlement_accept_payload(cur, payment, billing_month, base, remaining, is_refund=False):
    """Build a settlement payment-accepted message for one CASH row
    (SETTLEMENT_EXTRA or DEPOSIT_REFUNDED — never ADJUSTMENT/DEPOSIT_UTILIZED).

    Same shape as the rent/deposit acceptance messages:
      SETTLEMENT: base = receivable just before this receipt
                  (= remaining now + this amount); Full Clear vs Baki.
      REFUND:     base = cash refunded before this receipt;
                  total = base + this amount; baki = remaining refundable now.
    A full clear/refund appends the exclusive settlement-clear line.
    `base`/`remaining` come from settlement math computed AFTER the payment
    was recorded (passed in — this module must not import gui_api).
    """
    tid = payment['tenant_id']
    tenant = _tenant(cur, tid)
    cc = tenant.get('country_code') or '91'
    name = tenant['tenant_name']
    address = tenant['display_address'] or ""
    mode = str(payment.get('payment_mode') or 'CASH').upper()
    paid = float(payment['amount'] or 0)
    paid_date = _fmt_date(payment.get('payment_date'))
    month_label = datetime.strptime(billing_month, "%Y-%m").strftime("%B-%Y")

    if not is_refund:
        if remaining <= 0.01:
            result, diff = "Full Clear", 0.0
            clear = "\n\n✓ Full & Final Settlement Clear — no dues. Thank you!"
        else:
            result, diff = "Baki", remaining
            clear = ""
        vals = {
            "address": address, "name": name, "paid_date": paid_date,
            "paid_amount": _fmt_amount(paid), "mode": mode, "month": month_label,
            "base": _fmt_amount(base), "paid": _fmt_amount(paid),
            "diff": _fmt_amount(diff), "result": result, "clear_line": clear,
        }
        message = render_template("notify_accept_settlement", **vals)
        kind = "SETTLEMENT"
    else:
        total = base + paid
        if remaining <= 0.01:
            clear = "\n\n✓ Full & Final Settlement Clear — refund complete. Thank you!"
        else:
            clear = ""
        vals = {
            "address": address, "name": name, "paid_date": paid_date,
            "paid_amount": _fmt_amount(paid), "mode": mode, "month": month_label,
            "before": _fmt_amount(base), "paid": _fmt_amount(paid),
            "total": _fmt_amount(total), "baki": _fmt_amount(remaining),
            "clear_line": clear,
        }
        message = render_template("notify_accept_refund", **vals)
        kind = "REFUND"
    return {"message": message, "wa_link": wm.wa_link(tenant['phone_number'], message, cc),
            "kind": kind, "tenant_id": tid, "phone_number": tenant['phone_number']}


def reminder_payload(cur, reminder, rem_type='rent', paid_amount=None, paid_date=None):
    """Build NOTIFY/D1/DUE messages for one tenant_reminders row.

    All wording comes from the DB template master (reminder_templates),
    keyed by kind x type: notify_paid_<type>, notify_set_<type>,
    d1_<type>, due_<type>. D-day for rent additionally carries the full
    hisab message (sent as a separate HISAB item by the sender)."""
    tenant = _tenant(cur, reminder['tenant_id'])
    name = tenant['tenant_name']
    address = tenant['display_address'] or ""
    amount = _fmt_amount(reminder.get('amount_due'))
    rdate = _fmt_date(reminder['reminder_date'])
    month = _fmt_month(reminder['billing_month'])
    header = f"{name}"
    if address:
        header += f" ({address})"

    vals = {
        "header": header,
        "name": name,
        "address": address,
        "due_date": rdate,
        "amount": amount,
        "month": month,
        "paid_date": _fmt_date(paid_date) if paid_date else "",
        "paid_amount": f"{float(paid_amount):,.0f}" if paid_amount else "",
    }

    # NOTIFY: payment acknowledgment vs plain due-date set
    if paid_amount is not None and float(paid_amount) > 0:
        notify = render_template(f"notify_paid_{rem_type}", **vals)
    else:
        notify = render_template(f"notify_set_{rem_type}", **vals)

    # D1 / DUE: date + amount + type only
    d1 = render_template(f"d1_{rem_type}", **vals)
    due = render_template(f"due_{rem_type}", **vals)

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
        "notify": notify,
        "d1": d1,
        "due": due,
        "hisab": hisab,
        "wa_link": wm.wa_link(tenant['phone_number'], notify, tenant.get('country_code') or '91'),
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


def billing_month_reminders(billing_month):
    """ACTIVE reminders booked against one billing month, whatever calendar
    month their due date falls in. A reminder belongs to the billing cycle
    whose dues it collects; the due date is only when collection happens."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, t.tenant_name, r2.display_address,
               CASE WHEN r.note ILIKE '%%deposit%%' THEN 'deposit' ELSE 'rent' END AS rem_type
        FROM rental_core.tenant_reminders r
        JOIN rental_core.tenants t ON r.tenant_id = t.id
        LEFT JOIN rental_core.rooms r2 ON t.room_id = r2.id
        WHERE r.billing_month = %s
        ORDER BY r.status = 'ACTIVE' DESC, r.reminder_date ASC, r.id DESC
    """, (billing_month,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def due_now_reminders():
    """Action list: ACTIVE reminders due on or before the end of the current
    calendar month (overdue + due-now), any billing month. Anchored to today,
    never to the selected billing month."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, t.tenant_name, r2.display_address,
               CASE WHEN r.note ILIKE '%%deposit%%' THEN 'deposit' ELSE 'rent' END AS rem_type
        FROM rental_core.tenant_reminders r
        JOIN rental_core.tenants t ON r.tenant_id = t.id
        LEFT JOIN rental_core.rooms r2 ON t.room_id = r2.id
        WHERE r.status = 'ACTIVE'
          AND r.reminder_date <= (date_trunc('month', CURRENT_DATE) + INTERVAL '1 month' - INTERVAL '1 day')::date
        ORDER BY r.reminder_date ASC, r.id DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows