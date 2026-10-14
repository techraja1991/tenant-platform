#!/usr/bin/env python3
"""Automated next-due-date reminder sender — v2 (reliable).

Lifecycle
---------
* NOTIFY — queued instantly when a payment + due-date is saved (gui_api
  POST /api/reminders with paid_amount/paid_date).  The message is
  pre-rendered (notify_paid_rent / notify_paid_deposit / notify_set_*)
  mentioning the amount just received, the next due date and the pending
  balance.  Sent by the *instant* run (``--kind instant``).

* D1  — one day before reminder_date (reminder_date = today+1)
* DUE — on reminder_date (reminder_date = today)

  Both are generated on-the-fly from template master (d1_rent/due_rent
  etc.) and sent by the *scheduled* run (systemd daily at master time).

Verification
------------
A single JS probe (``_WA_STATUS_PROBE_JS``) is injected via
``context.add_init_script`` and is the **only** place that interprets
WhatsApp's delivery ticks.  Every Python check calls that same function.

  sent / delivered / read  →  SENT   (✓ or ✓✓)
  clock                    →  PENDING (clock icon, not yet on server)
  bubble-not-found / unknown / probe-failed → diagnosed as FAILED

If clock persists for the full verification window (30 s fast / 60 s
scheduled) the message is marked FAILED and retried once — up to
``MAX_ATTEMPTS = 2`` total attempts per (reminder_id, send_type).  The
reminder's stamp column (notify/d1/due_sent_at) is set **only** on SENT
or after the final failed attempt, so a transient failure does not
block the retry and stays visible in reminder_send_log.

Batch isolation
---------------
Each recipient is wrapped in its own ``try/except`` — an exception for
one tenant never aborts the remaining queue.  If the Playwright page
crashes it is re-created for the next item.

Usage:
  python send_due_reminders.py [--dry-run] [--limit N]
      [--min-delay 60] [--max-delay 180] [--month YYYY-MM] [--test-number ...]
      [--kind instant|scheduled] [--fast] [--headed]
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, date, timedelta

from db_config import get_db_connection
import whatsapp_message as wm
import reminder_message as rm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from send_whatsapp_reminders import (  # noqa: E402
    new_browser, paste_text, human_type, random_pause,
    SELECTOR_CHAT_LIST, SELECTOR_INPUT, SELECTOR_SEND,
)

_BATCH = None
_HEADED = False
MAX_ATTEMPTS = 2          # total attempts per (reminder_id, send_type)
VERIFY_FAST_MS = 30000
VERIFY_SCHED_MS = 60000
# Single grey tick: the message left our device but has not reached the
# recipient's yet. Recorded as SENT with this warning (not an error).
_SEND_WARNING = ("Sent from our end (single tick) — "
                 "not yet delivered to recipient")

# ---------------------------------------------------------------------------
# Single-source-of-truth delivery probe
# ---------------------------------------------------------------------------
_WA_STATUS_PROBE_JS = r"""
(() => {
  function normText(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

  function outgoingRows() {
    const main = document.querySelector('#main');
    if (!main) return [];
    let rows = [...main.querySelectorAll('[data-id^="true_"]')];
    if (rows.length === 0) rows = [...main.querySelectorAll('.message-out')];
    if (rows.length === 0) rows = [...main.querySelectorAll('[data-testid="msg-container"]')];
    return rows;
  }

  function findRow(snip) {
    const rows = outgoingRows();
    if (rows.length === 0) return null;
    const suf = snip && snip.suf, pre = snip && snip.pre;
    let mine = null;
    if (suf) mine = rows.filter(r => normText(r.innerText).includes(suf)).pop();
    if (!mine && pre) mine = rows.filter(r => normText(r.innerText).includes(pre)).pop();
    return mine || null;
  }

  // The delivery-tick node lives in the msg-meta footer. Scope every lookup
  // there first (falling back to the whole bubble) so decorative SVGs in the
  // bubble body (e.g. the tail-out corner) can never shadow the tick marker.
  function scopesFor(mine) {
    const meta = mine.querySelector('[data-testid="msg-meta"], [data-testid="msg-status"]');
    return meta ? [meta, mine] : [mine];
  }

  function status(snip) {
    const mine = findRow(snip);
    if (!mine) return 'bubble-not-found';
    const scopes = scopesFor(mine);

    // 1) aria-labels on the tick node (tooltips: Sent / Delivered / Read / Pending).
    //    NOTE: current WhatsApp Web pads them with spaces (" Sent ") — trim first.
    //    Ground truth (Oct-2026 DOM): single grey tick = aria " Sent ".
    for (const scope of scopes) {
      const ariaNodes = [...scope.querySelectorAll('[aria-label]')];
      for (const n of ariaNodes) {
        const label = (n.getAttribute('aria-label') || '').trim().toLowerCase();
        if (!label || label === 'you:') continue;
        if (/pending|not sent|waiting|clock/.test(label)) return 'clock';
        if (/read/.test(label)) return 'read';
        if (/deliver/.test(label)) return 'delivered';
        if (label === 'sent') return 'sent';   // single tick: left our device, not yet on recipient's
      }
    }

    // 2) data-icon on the tick node (wds-ic-* in current builds, msg-*/dblcheck in older).
    //    Per Oct-2026 ground truth the single-tick icon is check-styled, so any
    //    check/tick icon WITHOUT a dbl marker counts as single ('sent'); a real
    //    device-delivery announces itself via aria "Delivered" handled above.
    for (const scope of scopes) {
      const icons = [...scope.querySelectorAll('[data-icon]')];
      for (const el of icons) {
        const icon = (el.getAttribute('data-icon') || '').toLowerCase();
        if (!icon || icon.includes('tail')) continue;
        if (icon.includes('time') || icon.includes('clock') || icon.includes('pending')) return 'clock';
        if (icon.includes('dblcheck-ack') || icon.includes('read')) return 'read';
        if (icon.includes('dblcheck')) return 'delivered';
        if (icon.includes('sent') || icon.includes('check') || icon.includes('tick') || icon.includes('deliver')) return 'sent';
      }
    }

    // 3) svg <title> inside msg-meta (current single-tick icon is titled
    //    "wds-ic-delivered" = delivered-to-server, i.e. single grey tick).
    for (const scope of scopes) {
      const titles = [...scope.querySelectorAll('svg title')].map(t => (t.textContent || '').toLowerCase());
      for (const t of titles) {
        if (!t || t.includes('tail')) continue;
        if (/pending|clock|time/.test(t)) return 'clock';
        if (/read/.test(t)) return 'read';
        if (/sent|tick|check|deliver/.test(t)) return 'sent';
      }
    }

    const html = mine.innerHTML || '';
    const text = mine.innerText || mine.textContent || '';
    if (/[\u2713\u2714\u2705]/.test(text) || /[\u2713\u2714\u2705]/.test(html)) return 'sent';
    return 'unknown';
  }

  window.__waMsgStatus = status;
  window.__waReady = function (snip) {
    const s = status(snip);
    return s !== 'unknown' && s !== 'bubble-not-found' && s !== 'clock';
  };
  window.__waSnippet = function (snip) {
    // Diagnostic snippet: the msg-meta footer (tick node) + tick labels,
    // so a future 'unknown' carries the actual marker in the log.
    const mine = findRow(snip);
    if (!mine) return '';
    const scopes = scopesFor(mine);
    const meta = scopes[0];
    const bits = [];
    bits.push('META:' + (meta.outerHTML || '').slice(0, 1500));
    bits.push('TITLES:' + [...mine.querySelectorAll('svg title')].map(t => t.textContent).join('|').slice(0, 200));
    bits.push('LABELS:' + [...mine.querySelectorAll('[aria-label]')].map(n => n.getAttribute('aria-label')).join('|').slice(0, 200));
    return bits.join(' ');
  };
})();
"""


def _norm_snippet(message):
    import re as _re
    norm = _re.sub(r'[^a-z0-9]', '', (message or '').lower())
    return {"suf": norm[-40:], "pre": norm[:40]}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def log_reminder_send(batch_id, reminder, send_type, message, status, error=None, screenshot_b64=None):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO rental_core.reminder_send_log
        (batch_id, reminder_id, tenant_id, phone_number, send_type,
         message, status, error, sent_at, screenshot)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        batch_id, reminder['id'], reminder['tenant_id'], reminder['phone_number'],
        send_type, message, status, error,
        datetime.utcnow() if status == "SENT" else None,
        screenshot_b64,
    ))
    conn.commit()
    cur.close()
    conn.close()


def stamp_reminder_sent(reminder_id, col):
    if reminder_id is None:
        return  # payment-accepted sends have no reminder row to stamp
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE rental_core.tenant_reminders
           SET {col} = now(), updated_at = now()
         WHERE id = %s AND {col} IS NULL
    """, (reminder_id,))
    conn.commit()
    cur.close()
    conn.close()


def _pending_notifies(month=None):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    q = """
        SELECT l.id AS log_id, l.reminder_id, l.tenant_id, l.phone_number,
               l.message, l.send_type
        FROM rental_core.reminder_send_log l
        LEFT JOIN rental_core.tenant_reminders r ON l.reminder_id = r.id
        WHERE l.send_type = 'NOTIFY' AND l.status = 'QUEUED'
          AND (l.reminder_id IS NULL OR r.status = 'ACTIVE')
    """
    args = []
    if month:
        q += """ AND l.reminder_id IN (
                    SELECT id FROM rental_core.tenant_reminders WHERE billing_month = %s
                )"""
        args.append(month)
    q += " ORDER BY l.id ASC"
    cur.execute(q, args)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _retry_notifies(month=None):
    """NOTIFY rows that FAILED on first attempt (attempts < MAX) and have no
    QUEUED successor yet — returned as synthetic retry items so the retry
    happens without manual re-queue."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    # count FAILED per reminder; create retry for those with 1 failure and no QUEUED/SENT
    q = """
        SELECT r.id AS reminder_id, r.tenant_id, r.billing_month, r.reminder_date,
               t.phone_number, l.message, l.id AS failed_log_id
        FROM rental_core.tenant_reminders r
        JOIN rental_core.tenants t ON t.id = r.tenant_id
        JOIN rental_core.reminder_send_log l ON l.reminder_id = r.id
        WHERE r.status = 'ACTIVE'
          AND l.send_type = 'NOTIFY' AND l.status = 'FAILED'
          AND r.notify_sent_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM rental_core.reminder_send_log l2
              WHERE l2.reminder_id = r.id AND l2.send_type='NOTIFY' AND l2.status IN ('QUEUED','SENT')
          )
          AND (
              SELECT COUNT(*) FROM rental_core.reminder_send_log l3
              WHERE l3.reminder_id = r.id AND l3.send_type='NOTIFY' AND l3.status='FAILED'
          ) < %s
    """
    args = [MAX_ATTEMPTS]
    if month:
        q += " AND r.billing_month = %s"
        args.append(month)
    q += " ORDER BY l.id ASC"
    cur.execute(q, args)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _due_reminders(month=None):
    today = date.today()
    target_days = {today + timedelta(days=1): "D1", today: "DUE"}
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    q = """
        SELECT * FROM rental_core.tenant_reminders
        WHERE status = 'ACTIVE'
    """
    args = []
    if month:
        q += " AND billing_month = %s"
        args.append(month)
    q += " ORDER BY reminder_date ASC, id ASC"
    cur.execute(q, args)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    out = []
    for r in rows:
        rd = r['reminder_date']
        if not isinstance(rd, date):
            rd = datetime.strptime(str(rd)[:10], "%Y-%m-%d").date()
        for d, typ in target_days.items():
            col = {"D1": "d1_sent_at", "DUE": "due_sent_at"}[typ]
            if rd == d and r[col] is None:
                # respect MAX_ATTEMPTS — skip if already failed twice
                if _failed_count(r['id'], typ) >= MAX_ATTEMPTS:
                    print(f"⏭  SKIP tenant {r['tenant_id']} {typ} id={r['id']}: already failed {MAX_ATTEMPTS} times")
                    continue
                out.append((typ, r))
    return out


def _failed_count(reminder_id, send_type):
    """How many FAILED attempts for this (reminder_id, send_type)."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    # HISAB shares due_sent_at with DUE — count both together for DUE
    if send_type == "DUE":
        cur.execute("""
            SELECT COUNT(*) AS c FROM rental_core.reminder_send_log
            WHERE reminder_id=%s AND send_type IN ('DUE','HISAB') AND status='FAILED'
        """, (reminder_id,))
    else:
        cur.execute("""
            SELECT COUNT(*) AS c FROM rental_core.reminder_send_log
            WHERE reminder_id=%s AND send_type=%s AND status='FAILED'
        """, (reminder_id, send_type))
    c = cur.fetchone()['c'] if cur.rowcount else 0
    cur.close()
    conn.close()
    return int(c or 0)


def _has_been_sent(reminder_id, send_type):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM rental_core.reminder_send_log
        WHERE reminder_id=%s AND send_type=%s AND status='SENT' LIMIT 1
    """, (reminder_id, send_type))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def _outstanding_gt_zero(cur, tenant_id, billing_month, rem_type='rent'):
    """Rent → monthly_ledgers.new_baki ; Deposit → pending deposit."""
    if rem_type == 'deposit':
        # deposit pending = required - received
        try:
            cur.execute("""
                SELECT t.security_deposit_applied AS required,
                       COALESCE(SUM(p.amount),0) AS received
                FROM rental_core.tenants t
                LEFT JOIN rental_core.tenant_payments p
                  ON p.tenant_id=t.id AND p.payment_type='DEPOSIT'
                WHERE t.id=%s GROUP BY t.id
            """, (tenant_id,))
            row = cur.fetchone()
            if not row:
                return True
            pending = float(row['required'] or 0) - float(row['received'] or 0)
            return pending > 0.5
        except Exception:
            return True
    out = rm.current_outstanding(cur, tenant_id, billing_month)
    if out is None:
        return True
    try:
        return float(out or 0) > 0
    except Exception:
        return True


def render_notify(cur, log_row):
    return {
        "reminder_id": log_row['reminder_id'],
        "tenant_id": log_row['tenant_id'],
        "phone_number": log_row['phone_number'],
        "message": log_row['message'],
        "billing_month": None,
    }


def _rem_type_of(reminder):
    t = (reminder.get('rem_type') if isinstance(reminder, dict) else None) or ''
    if t in ('rent', 'deposit'):
        return t
    return 'deposit' if 'eposit' in str(reminder.get('note') or '') else 'rent'


_STAMP_COL = {"NOTIFY": "notify_sent_at", "D1": "d1_sent_at",
              "DUE": "due_sent_at", "HISAB": "due_sent_at"}


def _stamp_taken(conn_unused, reminder_id, col):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute(f"SELECT {col} FROM rental_core.tenant_reminders WHERE id=%s", (reminder_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return bool(row and row[col] is not None)


def render_due(cur, typ, reminder):
    payload = rm.reminder_payload(cur, reminder, rem_type=_rem_type_of(reminder))
    return {
        "reminder_id": reminder['id'],
        "tenant_id": reminder['tenant_id'],
        "phone_number": payload['phone_number'],
        "message": payload["d1"] if typ == "D1" else payload["due"],
        "billing_month": reminder['billing_month'],
    }


def build_items(month=None, notify_only=False):
    items = []
    seen = set()
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    # 1) pending NOTIFY (QUEUED). Payment-accepted rows share
    # reminder_id NULL, so key them by log row to avoid collapsing.
    for n in _pending_notifies(month):
        key = (n["reminder_id"], "NOTIFY") if n["reminder_id"] is not None else ("pay", n["log_id"], "NOTIFY")
        if key in seen:
            continue
        seen.add(key)
        it = render_notify(cur, n)
        it["send_type"] = "NOTIFY"
        it["log_id"] = n["log_id"]
        it["label"] = "NOTIFY"
        it["stamp_col"] = _STAMP_COL["NOTIFY"]
        items.append(it)
    # 1b) retry NOTIFY (FAILED once, still ACTIVE, not yet SENT/QUEUED)
    if not notify_only:
        # also consider retry notifies for scheduled runs that may have missed instant
        pass
    # include retry notifies for both instant and scheduled so instant recovers quickly
    for r_ in _retry_notifies(month):
        key = (r_["reminder_id"], "NOTIFY")
        if key in seen:
            continue
        seen.add(key)
        it = {
            "reminder_id": r_["reminder_id"],
            "tenant_id": r_["tenant_id"],
            "phone_number": r_["phone_number"],
            "message": r_["message"],
            "billing_month": r_["billing_month"],
            "send_type": "NOTIFY",
            "label": "NOTIFY-RETRY",
            "stamp_col": _STAMP_COL["NOTIFY"],
            "is_retry": True,
        }
        items.append(it)
    if notify_only:
        cur.close(); conn.close()
        return items
    # 2) D1 / DUE
    for typ, r in _due_reminders(month):
        key = (r['id'], typ)
        if key in seen:
            continue
        seen.add(key)
        rt = _rem_type_of(r)
        if not _outstanding_gt_zero(cur, r['tenant_id'], r['billing_month'], rt):
            print(f"⏭  SKIP tenant {r['tenant_id']} for {r['billing_month']} ({rt}): outstanding is 0 (already paid)")
            continue
        it = render_due(cur, typ, r)
        it["send_type"] = typ
        it["label"] = typ
        it["stamp_col"] = _STAMP_COL[typ]
        it["reminder_status"] = r.get('status', 'ACTIVE')
        items.append(it)
        if typ == "DUE" and rt == "rent":
            hisab = rm.reminder_payload(cur, r, rem_type=rt)["hisab"]
            if hisab:
                # Single bubble: DUE + hisab, stamped once via due_sent_at
                it["message"] = it["message"] + "\n\n" + hisab
                it["label"] = "DUE+HISAB"
    cur.close()
    conn.close()
    return items


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

def _capture_screenshot(page):
    try:
        import base64 as _b64
        return _b64.b64encode(page.screenshot(full_page=False)).decode('utf-8')
    except Exception:
        return None


def _ensure_page(context, page):
    """Return a live page; recreate if closed/crashed."""
    try:
        if page and not page.is_closed():
            return page
    except Exception:
        pass
    try:
        page = context.pages[0] if context.pages else context.new_page()
        return page
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core sender — per-item isolated, never aborts batch
# ---------------------------------------------------------------------------

def send_items(items, min_delay=60, max_delay=180, dry_run=False, limit=None,
               test_number=None, fast=False):
    global _TYPE_MIN, _TYPE_MAX, _DIAG
    if fast:
        _TYPE_MIN, _TYPE_MAX = 8, 20
    else:
        _TYPE_MIN, _TYPE_MAX = 40, 140
    if dry_run:
        print("\n--- DRY RUN (no messages sent) ---")
        for i, it in enumerate(items):
            if limit and i >= limit:
                print(f"🛑 Reached limit {limit}, stopping.")
                break
            target = wm.normalize_phone(test_number) if test_number else it['phone_number']
            print(f"[{it['label']}] tenant {it['tenant_id']} -> {target}: message ready")
        return
    if test_number:
        print(f"🧪 TEST MODE — messages sent to dummy {wm.normalize_phone(test_number)}.")
    pw, context = new_browser(headed=_HEADED)
    context.add_init_script(_WA_STATUS_PROBE_JS)
    page = context.pages[0] if context.pages else context.new_page()
    print("🔵 Launching WhatsApp Web session...")
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SELECTOR_CHAT_LIST, timeout=60000)
    except Exception:
        print("❌ Not logged in. Run: python send_whatsapp_reminders.py --login")
        try:
            for sk in items:
                rem2 = {
                    "id": sk["reminder_id"], "tenant_id": sk["tenant_id"],
                    "phone_number": sk["phone_number"], "billing_month": sk["billing_month"],
                }
                log_reminder_send(_BATCH, rem2, sk["send_type"], sk.get("message"),
                                  "SKIPPED", error="WhatsApp Web not logged in - session expired")
        except Exception:
            pass
        context.close()
        pw.stop()
        sys.exit(2)
    print("✅ WhatsApp Web authenticated.")

    sent = failed = skipped = 0
    _attempted = set()

    for i, it in enumerate(items):
        # ---- limit / dedup / pre-checks (isolated) ----
        if limit and i >= limit:
            print(f"🛑 Reached limit {limit}, stopping.")
            break
        if i > 0:
            random_pause(min_delay, max_delay)

        num = wm.normalize_phone(test_number) if test_number else wm.normalize_phone(it["phone_number"])
        # Payment-accepted NOTIFYs share reminder_id NULL — key those by log row
        # so distinct messages are not collapsed into one attempt per run.
        _akey = ((it["reminder_id"], it["send_type"]) if it.get("reminder_id") is not None
                 else ("pay", it.get("log_id"), it["send_type"]))
        if _akey in _attempted:
            print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: already attempted this run")
            skipped += 1
            continue
        _attempted.add(_akey)

        # Re-check stamp (another run may have completed it while we were sleeping)
        if it["send_type"] in ("D1", "DUE", "HISAB"):
            if _stamp_taken(None, it["reminder_id"], it.get("stamp_col", "d1_sent_at")):
                # But if last run FAILED (stamp after max attempts), don't re-send
                # _due_reminders already excluded max-attempt items, so this is genuine SENT
                print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: already stamped")
                skipped += 1
                continue

        # Verify reminder still ACTIVE (not superseded/deleted).
        # Payment-accepted NOTIFYs have reminder_id NULL (no reminder row) — skip check.
        if it.get("reminder_id") is not None:
            try:
                conn_chk = get_db_connection(as_dict=True); _c = conn_chk.cursor()
                _c.execute("SELECT status FROM rental_core.tenant_reminders WHERE id=%s", (it["reminder_id"],))
                _row = _c.fetchone()
                _c.close(); conn_chk.close()
                if not _row or _row["status"] != "ACTIVE":
                    # NOTIFY-RETRY synthetic items may have lost their reminder; skip
                    print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: reminder no longer ACTIVE")
                    skipped += 1
                    continue
            except Exception:
                pass

        # ---- per-item send with full isolation ----
        # We attempt at most 2 tries *within this run* if first returns clock,
        # but each is a fresh paste+send so WhatsApp creates a new bubble.
        # Overall attempts are bounded by MAX_ATTEMPTS across runs via DB count.
        item_sent = False
        last_diag = None
        last_screenshot = None
        last_error = None

        # Ensure page is live before this item
        page = _ensure_page(context, page)
        if page is None:
            print(f"   ❌ Page lost before sending to tenant {it['tenant_id']}, recreating...")
            try:
                page = context.new_page()
                page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
                page.wait_for_selector(SELECTOR_CHAT_LIST, timeout=30000)
            except Exception as e:
                print(f"   ❌ Cannot recreate page: {e} — marking item FAILED")
                reminder = {"id": it["reminder_id"], "tenant_id": it["tenant_id"], "phone_number": it["phone_number"], "billing_month": it["billing_month"]}
                if it.get("log_id"):
                    _mark_log_failed(it["log_id"], f"Page crashed before send: {e}", None)
                elif it.get("is_retry"):
                    log_reminder_send(_BATCH, reminder, it["send_type"], it.get("message"), "FAILED", str(e))
                else:
                    log_reminder_send(_BATCH, reminder, it["send_type"], it.get("message"), "FAILED", str(e))
                failed += 1
                continue

        # Determine how many intra-run attempts to allow:
        # If already has 1 FAILED in DB, this run is the 2nd and final attempt → 1 try.
        # If 0 FAILED, allow up to 2 tries within run (immediate retry on clock).
        already_failed = _failed_count(it["reminder_id"], it["send_type"]) if not it.get("log_id") else 0
        # For NOTIFY with log_id, attempts tracked via that log's retry chain; allow 1 retry within run on clock
        intra_attempts = 1 if already_failed >= 1 else 2

        for attempt in range(1, intra_attempts + 1):
            try:
                ct = " (retry {})".format(attempt) if attempt > 1 else ""
                print(f"📨 [{i+1}/{len(items)}]{ct} {it['label']} to tenant {it['tenant_id']} {num}...")
                page.goto(f"https://web.whatsapp.com/send?phone={num}", wait_until="domcontentloaded")
                page.wait_for_selector(SELECTOR_INPUT, timeout=30000)
                composer = page.locator(SELECTOR_INPUT).first
                composer.click()
                time.sleep(random.uniform(0.5, 1.5))
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                if fast:
                    human_type(page, it["message"])
                else:
                    paste_text(page, it["message"])
                time.sleep(0.5)
                time.sleep(random.uniform(0.4, 1.2))
                page.locator(SELECTOR_SEND).first.click()
                time.sleep(random.uniform(1.0, 2.0))
                time.sleep(random.uniform(0.5, 1.0))

                snip = _norm_snippet(it["message"])
                _verify_ms = VERIFY_FAST_MS if fast else VERIFY_SCHED_MS

                try:
                    page.wait_for_function("(s) => window.__waReady && window.__waReady(s)",
                                           arg=snip, timeout=_verify_ms)
                    delivered = True
                    _DIAG = page.evaluate("(s) => window.__waMsgStatus(s)", snip)
                except Exception:
                    try:
                        status = page.evaluate("(s) => window.__waMsgStatus(s)", snip)
                    except Exception:
                        status = 'probe-failed'
                    _DIAG = status
                    delivered = status in ('sent', 'delivered', 'read')

                screenshot_b64 = _capture_screenshot(page)
                last_screenshot = screenshot_b64
                last_diag = _DIAG

                if not delivered:
                    try:
                        time.sleep(1.5)
                        status2 = page.evaluate("(s) => window.__waMsgStatus(s)", snip)
                        if status2 in ('sent', 'delivered', 'read'):
                            delivered = True
                            _DIAG = status2
                            last_diag = status2
                            print("   ✅ Late check: delivery tick now visible - marking SENT")
                    except Exception:
                        pass

                debug_html = None
                if not delivered and _DIAG == 'unknown':
                    try:
                        debug_html = page.evaluate("(s) => window.__waSnippet(s)", snip)
                    except Exception:
                        debug_html = None

                if delivered:
                    item_sent = True
                    break  # success → no more intra-run retries
                else:
                    # Not delivered — decide whether to intra-run retry
                    reason_map = {
                        'clock': 'WhatsApp still shows the pending clock icon - message has not left the device yet',
                        'bubble-not-found': 'Could not find the sent message bubble in the chat pane',
                        'unknown': 'Message bubble found but no known delivery-status marker matched (raw bubble HTML attached)',
                        'probe-failed': 'Delivery-status probe crashed while inspecting the page',
                    }
                    last_error = reason_map.get(_DIAG, 'Delivery not confirmed in chat')
                    if debug_html:
                        last_error += "\n\n--- debug: matched bubble HTML (first 1200 chars) ---\n" + debug_html
                    # Only retry within run on clock with attempts left
                    if _DIAG == 'clock' and attempt < intra_attempts:
                        print(f"   ⏳ Clock still pending, retrying immediately (attempt {attempt+1}/{intra_attempts})...")
                        time.sleep(3)
                        continue
                    else:
                        break  # give up this run, let DB retry logic handle next scheduled run if attempts remain

            except Exception as e:
                last_error = str(e)
                last_diag = 'exception'
                last_screenshot = _capture_screenshot(page)
                print(f"   ❌ Exception on attempt {attempt}: {e}")
                # brief pause before intra-run retry if not last attempt
                if attempt < intra_attempts:
                    time.sleep(3)
                    # recreate page if it died
                    page = _ensure_page(context, page)
                    if page is None:
                        try:
                            page = context.new_page()
                            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
                            page.wait_for_selector(SELECTOR_CHAT_LIST, timeout=30000)
                        except Exception:
                            break
                else:
                    break

        # ---- record result (always, never throw) ----
        try:
            if test_number:
                it = {**it, "phone_number": wm.normalize_phone(test_number)}
            reminder = {
                "id": it["reminder_id"], "tenant_id": it["tenant_id"],
                "phone_number": it["phone_number"], "billing_month": it["billing_month"],
            }
            if item_sent:
                # Single grey tick ('sent') = message left OUR device but is not yet
                # on the recipient's — still a success, recorded with a warning
                # instead of an error. Double tick / read ('delivered'/'read')
                # are plain SENT, exactly as before.
                warn = _SEND_WARNING if last_diag == 'sent' else None
                if it.get("log_id"):
                    _mark_log_sent(it["log_id"], last_screenshot, warning=warn)
                elif it.get("is_retry"):
                    # retry NOTIFY was not a QUEUED log — insert SENT directly
                    log_reminder_send(_BATCH, reminder, it["send_type"], it["message"], "SENT",
                                      error=warn, screenshot_b64=last_screenshot)
                else:
                    log_reminder_send(_BATCH, reminder, it["send_type"], it["message"], "SENT",
                                      error=warn, screenshot_b64=last_screenshot)
                stamp_reminder_sent(it["reminder_id"], it.get("stamp_col", "notify_sent_at"))
                sent += 1
                print(f"   ✅ Sent {it['label']} to tenant {it['tenant_id']} (status: {last_diag})"
                      + (" — delivered-to-recipient unconfirmed" if warn else ""))
            else:
                # FAILED — only stamp if this was the final allowed attempt
                # (prevents retry starvation)
                failed_count_after = _failed_count(it["reminder_id"], it["send_type"])
                # For NOTIFY with log_id, failed_count is from new logic; use intra count
                will_exhaust = (failed_count_after + 1) >= MAX_ATTEMPTS
                # For synthetic retry items, count differently
                if it.get("is_retry"):
                    will_exhaust = True  # this was the retry, no further
                if it.get("log_id"):
                    _mark_log_failed(it["log_id"], last_error or f"Delivery unconfirmed (status: {last_diag})", last_screenshot)
                else:
                    log_reminder_send(_BATCH, reminder, it["send_type"], it["message"], "FAILED",
                                      error=last_error or f"Delivery unconfirmed (status: {last_diag})",
                                      screenshot_b64=last_screenshot)
                if will_exhaust and it["send_type"] in ("D1", "DUE", "HISAB"):
                    stamp_reminder_sent(it["reminder_id"], it.get("stamp_col", "d1_sent_at"))
                    print(f"   ℹ️  Max attempts reached — stamped {it['stamp_col']} to prevent further retries")
                elif it["send_type"] in ("D1", "DUE", "HISAB"):
                    print(f"   ❌ Delivery unconfirmed (status: {last_diag}) — will retry next run (attempt {failed_count_after+1}/{MAX_ATTEMPTS})")
                else:
                    # NOTIFY: if not exhausted, the retry helper will re-queue it
                    if not will_exhaust:
                        print(f"   ❌ NOTIFY failed (status: {last_diag}) — queued for retry")
                    else:
                        # final NOTIFY failure also stamps to hide from future 0-outstanding checks?
                        stamp_reminder_sent(it["reminder_id"], it.get("stamp_col", "notify_sent_at"))
                failed += 1
        except Exception as e:
            # Logging itself must not kill the batch
            print(f"   ❌ Failed to write log for tenant {it['tenant_id']}: {e}")
            failed += 1

    context.close()
    pw.stop()
    print(f"\n📊 Summary: sent={sent} failed={failed} skipped={skipped}")
    print("SEND_REPORT " + json.dumps({"batch_id": _BATCH, "sent": sent,
                                       "failed": failed, "skipped": skipped}))
    global _LAST_SUMMARY
    _LAST_SUMMARY = {"sent": sent, "failed": failed, "skipped": skipped}


def _mark_log_failed(log_id, error, screenshot_b64=None):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    if screenshot_b64:
        cur.execute("""
            UPDATE rental_core.reminder_send_log
               SET status = 'FAILED', error = %s, sent_at = now(), screenshot = %s
             WHERE id = %s AND status = 'QUEUED'
        """, (error, screenshot_b64, log_id))
    else:
        cur.execute("""
            UPDATE rental_core.reminder_send_log
               SET status = 'FAILED', error = %s, sent_at = now()
             WHERE id = %s AND status = 'QUEUED'
        """, (error, log_id))
    conn.commit()
    cur.close()
    conn.close()


def _mark_log_sent(log_id, screenshot_b64=None, warning=None):
    """QUEUED → SENT. `warning` (e.g. single-tick unconfirmed delivery) is
    stored in the error column as a non-error note; None clears nothing —
    it writes the warning only when given, else leaves error untouched."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    sets, args = ["status = 'SENT'", "sent_at = now()"], []
    if screenshot_b64:
        sets.append("screenshot = %s")
        args.append(screenshot_b64)
    if warning:
        sets.append("error = %s")
        args.append(warning)
    args.append(log_id)
    cur.execute(f"""
        UPDATE rental_core.reminder_send_log
           SET {", ".join(sets)}
         WHERE id = %s AND status = 'QUEUED'
    """, args)
    conn.commit()
    cur.close()
    conn.close()


_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_state.json")
_TEE = None
_LAST_SUMMARY = None
_DIAG = None
_SESSION_STARTED = None


class _StateTee:
    def __init__(self, orig):
        self._orig = orig
        self.run_id = None

    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def log_line(self, line):
        if not self.run_id:
            return
        try:
            import json as _json
            from datetime import datetime as _dt
            try:
                with open(_STATE_PATH) as f:
                    st = _json.load(f)
            except Exception:
                return
            if st.get("run_id") != self.run_id:
                return
            st.setdefault("lines", []).append(
                {"ts": _dt.now().strftime("%H:%M:%S"), "line": str(line)[:300]})
            st["lines"] = st["lines"][-200:]
            st["updated_at"] = _dt.now().isoformat()
            tmp = _STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                _json.dump(st, f)
            os.replace(tmp, _STATE_PATH)
        except Exception:
            pass


def _state_write(st):
    import json as _json
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(st, f)
    os.replace(tmp, _STATE_PATH)


def _state_init(kind):
    from datetime import datetime as _dt
    st = {"run_id": uuid.uuid4().hex[:10], "kind": kind, "pid": os.getpid(),
          "status": "running", "started_at": _dt.now().isoformat(),
          "updated_at": _dt.now().isoformat(), "lines": [], "summary": None}
    _state_write(st)
    return st


def _state_finish(status, summary=None, run_id=None):
    from datetime import datetime as _dt
    try:
        import json as _json
        with open(_STATE_PATH) as f:
            st = _json.load(f)
        if run_id and st.get("run_id") != run_id:
            return
        st["status"] = status
        st["finished_at"] = _dt.now().isoformat()
        st["updated_at"] = st["finished_at"]
        if summary is not None:
            st["summary"] = summary
        _state_write(st)
    except Exception:
        pass


_PAUSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_paused")
_RETRY_PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wa_sender_retry_pending")


def _spawn_background_retry(month=None):
    if os.path.exists(_RETRY_PENDING_FILE):
        return False
    try:
        open(_RETRY_PENDING_FILE, "w").close()
    except Exception:
        pass
    cmd = [sys.executable, os.path.abspath(__file__), "--kind", "instant", "--wait-lock"]
    if month:
        cmd += ["--month", month]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        try:
            os.remove(_RETRY_PENDING_FILE)
        except Exception:
            pass
        return False


def main():
    if os.path.exists(_PAUSE_FILE):
        import builtins
        builtins.print("⏸  Sender is PAUSED (.sender_paused present). Remove the file to resume.")
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--month", default=None, help="Filter to one billing month YYYY-MM")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--min-delay", type=int, default=60)
    ap.add_argument("--max-delay", type=int, default=180)
    ap.add_argument("--test-number", default=None)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--kind", default=None, choices=[None, "instant", "scheduled"],
                    help="Launcher hint; auto-detected when omitted.")
    ap.add_argument("--wait-lock", action="store_true",
                    help="Internal: used by background-retry hand-off.")
    args = ap.parse_args()
    if not args.kind:
        args.kind = "scheduled" if os.environ.get("INVOCATION_ID") else "instant"

    global _BATCH, _HEADED
    _HEADED = args.headed

    global _TEE
    import sys as _sys
    _TEE = _StateTee(_sys.__stdout__)
    _sys.stdout = _TEE
    import builtins as _builtins
    _raw_print = _builtins.print

    def print(*a, **k):  # noqa: A001
        _raw_print(*a, **k)
        try:
            _TEE.log_line(" ".join(str(x) for x in a))
        except Exception:
            pass

    _st = _state_init(args.kind)
    _TEE.run_id = _st["run_id"]
    _rid = _st["run_id"]

    import fcntl
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wa_sender.lock")
    with open(lock_path, "w") as lock_f:
        if args.kind == "instant" and args.wait_lock:
            try:
                os.remove(_RETRY_PENDING_FILE)
            except Exception:
                pass
            try:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
            except OSError:
                pass
        elif args.kind == "instant":
            acquired = False
            for _w in range(60):
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(1)
            if not acquired:
                handed_off = _spawn_background_retry(args.month)
                if handed_off:
                    print("↪️  Sender still busy after 60s — handed off to a background retry that will send as soon as it's free.")
                    _state_finish("retry-queued", {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
                else:
                    print("⏭  Sender still busy after 60s — a retry is already queued behind it.")
                    _state_finish("skipped-busy", {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
                return
        else:
            try:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
            except OSError:
                pass

        try:
            if args.kind == "instant":
                time.sleep(5)
            max_passes = 3
            for _pass in range(max_passes):
                items = build_items(args.month, notify_only=(args.kind == "instant"))
                print(f"🔎 Due-date reminder queue: {len(items)} message(s).")
                if not items:
                    print("Nothing to send.")
                    _state_finish("finished", {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
                    return
                if args.test_number and not args.dry_run and not args.limit:
                    args.limit = 1
                _BATCH = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
                _fast = True if args.kind == "instant" else args.fast
                _min_d = 10 if args.kind == "instant" else args.min_delay
                _max_d = 20 if args.kind == "instant" else args.max_delay
                send_items(items, _min_d, _max_d, args.dry_run, args.limit,
                           args.test_number, _fast)
                if args.dry_run or (args.limit and args.limit <= 1):
                    break
                # drain: if new items appeared during long send, loop again
                if _pass < max_passes - 1:
                    time.sleep(2)
        finally:
            _state_finish("finished", summary=_LAST_SUMMARY or {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
            fcntl.flock(lock_f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
