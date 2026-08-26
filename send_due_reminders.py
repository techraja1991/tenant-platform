#!/usr/bin/env python3
"""Automated next-due-date reminder sender.

Reads rental_core.tenant_reminders and sends the three reminder messages:

  * NOTIFY - queued whenever a reminder date is set/updated:
             "Your New Next Due Date is : <date>"
  * D1     - one day before the reminder date (reminder_date = today + 1)
  * DUE    - on the reminder date (reminder_date = today)

Each send is recorded in rental_core.reminder_send_log and the corresponding
sent_at column (notify/d1/due) is stamped so a message is never sent twice.

Reuses the WhatsApp Web session + anti-detection browser from
send_whatsapp_reminders.py (same profile dir, paste-based sending).

Usage:
  python send_due_reminders.py [--dry-run] [--limit N]
      [--min-delay 60] [--max-delay 180] [--month YYYY-MM] [--test-number ...]
"""
import argparse
import json
import os
import random
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
    log_send as _unused_log_send,
)

_BATCH = None
_HEADED = False


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
        JOIN rental_core.tenant_reminders r ON l.reminder_id = r.id
        WHERE l.send_type = 'NOTIFY' AND l.status = 'QUEUED'
          AND r.status = 'ACTIVE'
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
                out.append((typ, r))
    return out


def _outstanding_gt_zero(cur, tenant_id, billing_month):
    """Only send if the month's outstanding is actually > 0 (payment may have
    arrived before the due date, in which case no reminder goes out)."""
    out = rm.current_outstanding(cur, tenant_id, billing_month)
    return out is not None and float(out or 0) > 0


def render_notify(cur, log_row):
    """NOTIFY messages are pre-rendered at set time; return as-is."""
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
    """Build the send queue.

    notify_only=True (instant runs triggered by a payment/due-date save)
    dispatches ONLY pre-queued NOTIFY confirmations — campaign items
    (D1/DUE/HISAB) belong exclusively to scheduled / manual full runs,
    otherwise every save would re-fire every eligible reminder.
    """
    items = []
    seen = set()
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    for n in _pending_notifies(month):
        key = (n["reminder_id"], "NOTIFY")
        if key in seen:
            continue
        seen.add(key)
        it = render_notify(cur, n)
        it["send_type"] = "NOTIFY"
        it["log_id"] = n["log_id"]
        it["label"] = "NOTIFY"
        it["stamp_col"] = _STAMP_COL["NOTIFY"]
        items.append(it)
    if notify_only:
        return items
    for typ, r in _due_reminders(month):
        key = (r['id'], typ)
        if key in seen:
            continue
        seen.add(key)
        if not _outstanding_gt_zero(cur, r['tenant_id'], r['billing_month']):
            print(f"⏭  SKIP tenant {r['tenant_id']} for {r['billing_month']}: "
                  f"outstanding is 0 (already paid)")
            continue
        it = render_due(cur, typ, r)
        it["send_type"] = typ
        it["label"] = typ
        it["stamp_col"] = _STAMP_COL[typ]
        it["reminder_status"] = r.get('status', 'ACTIVE')
        items.append(it)
        if typ == "DUE":
            hkey = (r['id'], "HISAB")
            if hkey not in seen:
                seen.add(hkey)
                # on the due date, also send the full template hisab message
                hisab = rm.reminder_payload(cur, r, rem_type=_rem_type_of(r))["hisab"]
                if hisab:
                    h = dict(it)
                    h["send_type"] = "HISAB"
                    h["label"] = "HISAB"
                    h["message"] = hisab
                    h["stamp_col"] = _STAMP_COL["HISAB"]
                    items.append(h)
    cur.close()
    conn.close()
    return items


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
    page = context.pages[0] if context.pages else context.new_page()
    print("🔵 Launching WhatsApp Web session...")
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SELECTOR_CHAT_LIST, timeout=60000)
    except Exception:
        print("❌ Not logged in. Run: python send_whatsapp_reminders.py --login")
        # Leave originals QUEUED (auto-retry after re-login) but record a
        # SKIPPED row per item so the GUI log shows WHY nothing was sent.
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
    global _SESSION_STARTED
    _SESSION_STARTED = time.time()

    sent = failed = skipped = 0
    _attempted = set()
    for i, it in enumerate(items):
        if limit and i >= limit:
            print(f"🛑 Reached limit {limit}, stopping.")
            break
        if i > 0:
            random_pause(min_delay, max_delay)
        num = wm.normalize_phone(test_number) if test_number else wm.normalize_phone(it["phone_number"])
        _akey = (it["reminder_id"], it["send_type"])
        if _akey in _attempted:
            print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: already attempted this run")
            skipped += 1
            continue
        _attempted.add(_akey)
        # Pre-send guard: another run may have stamped this already, or the
        # reminder may have been superseded/deleted while we were launching.
        if it["send_type"] in ("D1", "DUE", "HISAB"):
            if _stamp_taken(None, it["reminder_id"], it.get("stamp_col", "d1_sent_at")):
                print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: already stamped")
                skipped += 1
                continue
        if it.get("reminder_status") == "ACTIVE" or it["send_type"] in ("D1", "DUE", "HISAB"):
            conn_chk = get_db_connection(as_dict=True); _c = conn_chk.cursor()
            _c.execute("SELECT status FROM rental_core.tenant_reminders WHERE id=%s", (it["reminder_id"],))
            _row = _c.fetchone()
            _c.close(); conn_chk.close()
            if not _row or _row["status"] != "ACTIVE":
                print(f"⏭  SKIP {it['label']} tenant {it['tenant_id']}: reminder no longer ACTIVE")
                skipped += 1
                continue
        try:
            print(f"📨 [{i+1}/{len(items)}] {it['label']} to tenant {it['tenant_id']} {num}...")
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
            # Verify delivery: the outgoing message must actually appear in
            # the chat pane (#main). Checking only the composer being cleared
            # is NOT enough — a failed paste also leaves it empty.
            import time as _t
            import re as _re
            _t.sleep(random.uniform(0.5, 1.0))
            # Two-stage delivery proof:
            #   1) our text sits in an OUTGOING bubble, AND
            #   2) that bubble shows a server-ack tick (single/double),
            #      NOT the clock. A rendered bubble with a clock is only
            #      queued locally — the recipient never gets it.
            # Comparison is alphanumeric-normalized so WhatsApp formatting
            # (bold/italic asterisks, emoji, punctuation) cannot break it.
            target = _re.sub(r'[^a-z0-9]', '', it["message"].lower())[-60:]
            # Freshly (re)linked sessions render slowly; give the first
            # sends / first 5 minutes a longer proof window.
            _warm = i == 0 or (_SESSION_STARTED and time.time() - _SESSION_STARTED < 300)
            _verify_ms = 150000 if _warm else 90000

            def _proof_wait(ms):
                page.wait_for_function(
                    """(snip) => {
                        const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                        const main = document.querySelector('#main');
                        if (!main) return false;
                        const outs = [...document.querySelectorAll('.message-out')];
                        const mine = outs.filter(b => norm(b.innerText).includes(snip)).pop();
                        if (!mine) return false;
                        const html = mine.innerHTML || '';
                        if (/status-pending|data-icon=\"clock\"/i.test(html)) return false;   // still queued locally
                        return /status-check|status-dblcheck|msg-check|msg-dblcheck/i.test(html); // server acked
                    }""",
                    arg=target,
                    timeout=ms,
                )

            try:
                try:
                    _proof_wait(_verify_ms)
                    delivered = True
                except Exception:
                    probe = 'bubble-not-found'
                    # Degraded/slow sessions sometimes never render the sent
                    # bubble even though WhatsApp accepted the message. Reopen
                    # the chat once — history re-renders from the server — and
                    # rescan before declaring failure.
                    try:
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_selector(SELECTOR_INPUT, timeout=20000)
                        time.sleep(2)
                        _proof_wait(30000)
                        delivered = True
                        probe = None
                    except Exception:
                        pass
                if not delivered:
                    raise AssertionError('verify-failed')
            except Exception:
                # Diagnose WHY for the failure record
                try:
                    probe = page.evaluate(
                        """(snip) => {
                            const norm=(s)=>(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
                            const outs=[...document.querySelectorAll('.message-out')];
                            const mine=outs.filter(b=>norm(b.innerText).includes(snip)).pop();
                            if(!mine) return 'bubble-not-found';
                            const h=mine.innerHTML||'';
                            if(/status-pending|data-icon=\"clock\"/i.test(h)) return 'stuck-pending-clock';
                            if(/status-check|status-dblcheck/i.test(h)) return 'acked-late';
                            return 'no-status-icons-detected';
                        }""", target)
                except Exception:
                    probe = 'probe-failed'
                _DIAG = probe
                delivered = False

            # Screenshot of the chat right after send attempt (proof)
            screenshot_b64 = None
            try:
                import base64 as _b64
                screenshot_b64 = _b64.b64encode(page.screenshot(full_page=False)).decode('utf-8')
            except Exception:
                pass

            if test_number:
                it = {**it, "phone_number": wm.normalize_phone(test_number)}
            reminder = {
                "id": it["reminder_id"], "tenant_id": it["tenant_id"],
                "phone_number": it["phone_number"], "billing_month": it["billing_month"],
            }
            if delivered:
                log_reminder_send(_BATCH, reminder, it["send_type"], it["message"], "SENT",
                                  screenshot_b64=screenshot_b64)
                if it.get("log_id"):
                    _mark_log_sent(it["log_id"])
                stamp_reminder_sent(it["reminder_id"], it.get("stamp_col", "notify_sent_at"))
                sent += 1
                print(f"   ✅ Sent {it['label']} to tenant {it['tenant_id']}")
            else:
                reason = {
                    'stuck-pending-clock': 'WhatsApp shows PENDING (clock) - message never reached server',
                    'bubble-not-found': 'Sent bubble not found in chat',
                    'acked-late': 'Ack arrived late',
                    'no-status-icons-detected': 'No delivery-status icons found on bubble',
                }.get(_DIAG, 'Delivery not confirmed in chat')
                log_reminder_send(_BATCH, reminder, it["send_type"], it["message"], "FAILED",
                                  error=reason, screenshot_b64=screenshot_b64)
                if it.get("log_id"):
                    _mark_log_failed(it["log_id"], reason)
                if _DIAG in ('stuck-pending-clock', 'bubble-not-found',
                             'acked-late', 'no-status-icons-detected') \
                        and it["send_type"] in ("D1", "DUE", "HISAB"):
                    # One attempt per stage, ever. All four diagnostics mean
                    # the click happened and WhatsApp accepted/queued the
                    # message client-side (delivery usually follows). Leaving
                    # these unstamped made every later run re-send them
                    # (duplicate-storm failure mode); failures stay visible in
                    # the send log instead.
                    stamp_reminder_sent(it["reminder_id"], it.get("stamp_col", "d1_sent_at"))
                    print(f"   ℹ️  stamped {it['stamp_col']} despite unconfirmed delivery (no cross-run retry)")
                failed += 1
                print(f"   ❌ Delivery unconfirmed for tenant {it['tenant_id']}")
        except Exception as e:
            print(f"   ❌ Failed for tenant {it['tenant_id']}: {e}")
            if test_number:
                it = {**it, "phone_number": wm.normalize_phone(test_number)}
            reminder = {
                "id": it["reminder_id"], "tenant_id": it["tenant_id"],
                "phone_number": it["phone_number"], "billing_month": it["billing_month"],
            }
            log_reminder_send(_BATCH, reminder, it["send_type"], it.get("message"), "FAILED", str(e))
            failed += 1

    context.close()
    pw.stop()
    print(f"\n📊 Summary: sent={sent} failed={failed} skipped={skipped}")
    print("SEND_REPORT " + json.dumps({"batch_id": _BATCH, "sent": sent,
                                       "failed": failed, "skipped": skipped}))
    global _LAST_SUMMARY
    _LAST_SUMMARY = {"sent": sent, "failed": failed, "skipped": skipped}


def _mark_log_failed(log_id, error):
    """Terminal FAILED on the original queued row — prevents drain-loop
    re-picks (duplicate sends) and phantom QUEUED entries."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        UPDATE rental_core.reminder_send_log
           SET status = 'FAILED', error = %s, sent_at = now()
         WHERE id = %s AND status = 'QUEUED'
    """, (error, log_id))
    conn.commit()
    cur.close()
    conn.close()


def _mark_log_sent(log_id):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        UPDATE rental_core.reminder_send_log
           SET status = 'SENT', sent_at = now()
         WHERE id = %s AND status = 'QUEUED'
    """, (log_id,))
    conn.commit()
    cur.close()
    conn.close()


_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_state.json")
_TEE = None
_LAST_SUMMARY = None
_DIAG = None
_SESSION_STARTED = None


class _StateTee:
    """Tees sender lines into .sender_state.json so the GUI can show live
    progress of whichever run is active (instant or scheduled)."""

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
                return  # a newer run owns the state file
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
    args = ap.parse_args()
    if not args.kind:
        # systemd services carry INVOCATION_ID; GUI/API spawns do not.
        args.kind = "scheduled" if os.environ.get("INVOCATION_ID") else "instant"

    global _BATCH, _HEADED
    _HEADED = args.headed

    # Live-run state for GUI monitoring
    global _TEE
    import sys as _sys
    _TEE = _StateTee(_sys.__stdout__)
    _sys.stdout = _TEE
    import builtins as _builtins
    _raw_print = _builtins.print

    def print(*a, **k):  # noqa: A001 - mirror every sender line to GUI state
        _raw_print(*a, **k)
        try:
            _TEE.log_line(" ".join(str(x) for x in a))
        except Exception:
            pass

    _st = _state_init(args.kind)
    _TEE.run_id = _st["run_id"]
    _rid = _st["run_id"]

    # Single-flight lock: two rapid triggers (e.g. deposit save followed
    # instantly by rent save) each spawn a sender; both would open the same
    # WhatsApp profile and the second would crash on the profile lock,
    # silently dropping its queued messages. Block here instead — the second
    # run waits for the first to finish, then picks up everything new.
    import fcntl
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wa_sender.lock")
    with open(lock_path, "w") as lock_f:
        if args.kind == "instant":
            # Instant triggers fire on EVERY payment/due-date save. If a
            # sender is already running, DO NOT queue up another browser
            # process — a backlog of lock-waiters re-sends everything
            # unstamped the moment the lock frees (the duplicate-storm
            # failure mode). The running drain pass picks up newly queued
            # NOTIFYs itself.
            try:
                fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("⏭  Sender already running — new NOTIFYs will be picked up by that run.")
                _state_finish("skipped-busy", {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
                return
        else:
            try:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
            except OSError:
                pass

        try:
            # Drain loop: messages can be queued while this run is still in
            # progress (queue was built before the browser finished booting).
            max_passes = 1 if args.kind == "instant" else 3
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
                send_items(items, args.min_delay, args.max_delay, args.dry_run, args.limit,
                           args.test_number, args.fast)
                if args.dry_run or (args.limit and args.limit <= 1):
                    break
        finally:
            _state_finish("finished", summary=_LAST_SUMMARY or {"sent": 0, "failed": 0, "skipped": 0}, run_id=_rid)
            fcntl.flock(lock_f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()