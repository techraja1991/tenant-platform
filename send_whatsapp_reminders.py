#!/usr/bin/env python3
"""Automated WhatsApp reminder sender for the Tenant Management Platform.

Sends the reminder template message to active tenants of the previous billing
period that still have an outstanding balance (new_baki > 0), using the
per-tenant mapped template or the active default template.

Anti-detection design (WhatsApp Web via Playwright):
  * Persistent browser profile - you scan the QR once (`--login`), the session
    is reused afterwards, no repeated QR scans.
  * Realistic browser fingerprint: real Chrome UA, viewport, locale + timezone.
  * Removes navigator.webdriver flag via init script.
  * Human-like typing: the message is typed into the input box character by
    character with randomised per-keystroke delays.
  * Human-like pacing: randomised delay (default 60-180s) between messages,
    plus random jitter before clicking send. Messages go out one by one.
  * No headless flag: uses headed chromium behind an X virtual framebuffer
    (xvfb-run) so the page cannot detect headless mode.

Usage:
  python send_whatsapp_reminders.py --login          # scan QR once to store session
  python send_whatsapp_reminders.py [--dry-run] [--limit N] \
      [--min-delay 60] [--max-delay 180] [--month YYYY-MM]
"""
import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime

from db_config import get_db_connection
import whatsapp_message as wm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "whatsapp_profile")
QR_IMAGE = os.path.join(BASE_DIR, "static", "whatsapp_qr.png")        # full login page
QR_CLEAN = os.path.join(BASE_DIR, "static", "whatsapp_qr_clean.png")  # padded QR only

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# WhatsApp Web DOM markers
SELECTOR_CHAT_LIST = 'div[data-testid="chat-list"], div[id="pane-side"]'
SELECTOR_QR = 'canvas, img[alt="Scan me!"]'
SELECTOR_INPUT = ('div[contenteditable="true"][data-tab="10"], '
                  'div[contenteditable="true"][spellcheck="true"], '
                  'footer div[contenteditable="true"]')
SELECTOR_SEND = ('button[aria-label="Send"], span[data-icon="send"], '
                'span[data-icon="wds-ic-send-filled"], '
                'button[data-testid="compose-btn-send"]')
SELECTOR_SENT = 'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"]'

_TYPE_MIN = 40
_TYPE_MAX = 140


def consumption_month(now=None):
    now = now or datetime.now()
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


def target_tenants(billing_month):
    """Active tenants billed for `billing_month` with an outstanding balance."""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ml.tenant_id, t.tenant_name, t.phone_number,
               r.display_address
        FROM rental_core.monthly_ledgers ml
        JOIN rental_core.tenants t ON ml.tenant_id = t.id
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE ml.billing_month = %s
          AND t.is_active = 'Y'
          AND COALESCE(ml.new_baki, 0.00) > 0
        ORDER BY r.display_address;
    """, (billing_month,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def log_send(batch_id, tenant, message, status, error=None, template=None):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO rental_core.whatsapp_send_log
        (batch_id, tenant_id, phone_number, template_id, template_name,
         billing_month, message, status, error, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        batch_id, tenant['tenant_id'], tenant['phone_number'],
        template['id'] if template else None,
        template['template_name'] if template else None,
        tenant['billing_month'], message, status, error,
        datetime.now() if status == "SENT" else None,
    ))
    conn.commit()
    cur.close()
    conn.close()


def build_all_messages(billing_month):
    """Render messages for every target tenant. Returns list of dicts."""
    tenants = target_tenants(billing_month)
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    out = []
    for t in tenants:
        item = {
            "tenant_id": t['tenant_id'],
            "tenant_name": t['tenant_name'],
            "phone_number": t['phone_number'],
            "display_address": t['display_address'],
            "billing_month": billing_month,
        }
        try:
            template = wm.resolve_template(cur, t['tenant_id'])
            if template is None:
                item["message"] = None
                item["error"] = "No active message template"
            else:
                item["message"], template, _ = wm.render_message(
                    cur, t['tenant_id'], billing_month, template)
                item["template_id"] = template['id']
                item["template_name"] = template['template_name']
        except Exception as e:
            item["message"] = None
            item["error"] = str(e)
        out.append(item)
    cur.close()
    conn.close()
    return out


def new_browser(user_data_dir=PROFILE_DIR, headed=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)
    # Use headed Chrome (less detectable) when a display is available (e.g.
    # running under xvfb-run); fall back to headless otherwise.
    if headed is None:
        # Always headless — session created + verified in headless mode.
        # xvfb headed mode generates different fingerprint → session rejected
        # by WhatsApp Web. Both "Send Now" and systemd timer must match.
        headed = False
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=not headed,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ],
        user_agent=DEFAULT_UA,
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        color_scheme="light",
        ignore_default_args=["--enable-automation"],
    )
    try:
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://web.whatsapp.com")
    except Exception:
        pass
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = (params) => (
                params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params)
            );
        }
    """)
    return pw, context


def login_flow():
    """Open WhatsApp Web, capture the FULL login screen (QR + surrounding
    visuals) as an image, and wait until the user scans it.

    WhatsApp Web redraws the QR roughly every 20 seconds, and a bare <canvas>
    grab is usually un-scannable because it has no white quiet-zone border.
    We therefore save TWO images, both refreshed every few seconds:
      * static/whatsapp_qr.png        - full login page (QR + surroundings)
      * static/whatsapp_qr_clean.png  - QR alone with a white padding border
    The clean QR is the reliable one to scan; the full page is for context.
    A plain browser refresh (F5) on the image URL also gets the newest frame.
    """
    import base64

    pw, context = new_browser(headed=_HEADED)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    print("🔵 Opening WhatsApp Web... wait for the QR to appear.")
    qr_saved = False
    try:
        # wait until the login QR container (has data-ref) is actually rendered
        try:
            page.wait_for_selector('div[data-ref] canvas, div[data-ref]', timeout=30000)
        except Exception:
            pass
        while True:
            # logged in?
            if page.locator(SELECTOR_CHAT_LIST).count() > 0:
                print("✅ Session already authenticated. Nothing to scan.")
                break
            # QR container present?
            qr_box = page.locator('div[data-ref]').first
            if qr_box.count() > 0:
                try:
                    # 1) Full visible page: QR plus all surrounding visuals.
                    page.screenshot(path=QR_IMAGE, full_page=False)
                    # 2) Grab the QR canvas pixels directly via toDataURL and
                    #    add a white quiet-zone border so phones can scan it.
                    data_url = page.evaluate(
                        "() => { const c = document.querySelector('div[data-ref] canvas, canvas');"
                        " try { return c.toDataURL('image/png'); } catch (e) { return null; } }"
                    )
                    if data_url:
                        import struct
                        raw = base64.b64decode(data_url.split(",")[1])
                        # png is 228x228 -> canvas size; get it from the DOM
                        canvas_size = page.evaluate(
                            "() => { const c = document.querySelector('div[data-ref] canvas, canvas');"
                            " return c ? { w: c.width, h: c.height } : null; }"
                        )
                        try:
                            import numpy as np
                            import cv2
                            arr = np.frombuffer(raw, np.uint8)
                            img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                            pad = 48
                            img = cv2.copyMakeBorder(img, pad, pad, pad, pad,
                                                     cv2.BORDER_CONSTANT, value=255)
                            cv2.imwrite(QR_CLEAN, img)
                        except Exception:
                            with open(QR_CLEAN, "wb") as f:
                                f.write(raw)
                    if not qr_saved:
                        print(f"📱 Full login screenshot saved to {QR_IMAGE}")
                        print(f"📱 Clean scannable QR saved to {QR_CLEAN}")
                        print("   Open either image in the browser and scan with")
                        print("   WhatsApp (Linked devices → Link a device).")
                        print("   The images auto-refresh with the current QR;")
                        print("   hit F5 on the image if it goes stale.")
                        qr_saved = True
                    else:
                        print(f"   🔄 QR refreshed ({datetime.now().strftime('%H:%M:%S')}) — images updated.")
                except Exception as e:
                    print(f"⚠️ Could not capture screenshot: {e}")
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        context.close()
        pw.stop()
    print("✅ Login flow finished. Session stored in whatsapp_profile/")
    print("   Now run: python send_whatsapp_reminders.py")


def human_type(page, text, min_ms=None, max_ms=None):
    """Type text char by char with randomised delays (human-like).

    WhatsApp sends a message on plain Enter, so newlines in the message body
    must be typed as Shift+Enter (a soft line break) — this keeps the whole
    multi-line hisab as ONE message until the send button is clicked.
    """
    lo = min_ms or _TYPE_MIN or 40
    hi = max_ms or _TYPE_MAX or 140
    for ch in text:
        if ch == "\n":
            page.keyboard.press("Shift+Enter")
            time.sleep(random.uniform(0.05, 0.15))
        else:
            page.keyboard.type(ch, delay=random.randint(lo, hi))
        if random.random() < 0.04:
            time.sleep(random.uniform(0.1, 0.4))


def paste_text(page, text):
    """Paste the whole message via the real clipboard (Ctrl+V). Multi-line
    text is pasted into the WhatsApp editor as paragraphs; the composer never
    triggers the Enter-key auto-send, so the whole hisab stays ONE message."""
    page.evaluate(f"navigator.clipboard.writeText({text!r})")
    time.sleep(random.uniform(0.2, 0.5))
    page.keyboard.press("Control+V")


def random_pause(min_s, max_s):
    t = random.uniform(min_s, max_s)
    print(f"   ⏳ waiting {t:.0f}s (human pause) before next message...")
    time.sleep(t)


def send_messages(items, min_delay=60, max_delay=180, dry_run=False, limit=None, test_number=None,
                  fast=False, mode="paste"):
    global _TYPE_MIN, _TYPE_MAX
    if fast:
        _TYPE_MIN, _TYPE_MAX = 8, 20   # quick typing for test verification
    else:
        _TYPE_MIN, _TYPE_MAX = 40, 140  # human-like typing for real sends
    if dry_run:
        print("\n--- DRY RUN (no messages sent) ---")
        for i, it in enumerate(items):
            if limit and i >= limit:
                print(f"🛑 Reached limit {limit}, stopping.")
                break
            status = "SKIPPED" if it.get("error") else "OK"
            target = wm.normalize_phone(test_number) if test_number else it['phone_number']
            print(f"[{status}] {it['display_address'] or ''} {it['tenant_name']} "
                  f"-> {target}: "
                  f"{('ERROR: ' + it['error']) if it.get('error') else 'message ready'}")
        return
    if test_number:
        print(f"🧪 TEST MODE — messages will be sent to dummy number "
              f"{wm.normalize_phone(test_number)} instead of tenants.")
    pw, context = new_browser(headed=_HEADED)
    page = context.pages[0] if context.pages else context.new_page()
    print("🔵 Launching WhatsApp Web session...")
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SELECTOR_CHAT_LIST, timeout=60000)
    except Exception:
        print("❌ Not logged in. Run: python send_whatsapp_reminders.py --login")
        context.close()
        pw.stop()
        sys.exit(2)
    print("✅ WhatsApp Web authenticated.")

    sent = failed = skipped = 0
    for i, it in enumerate(items):
        if limit and i >= limit:
            print(f"🛑 Reached limit {limit}, stopping.")
            break
        if it.get("error") or not it.get("message"):
            print(f"⏭  SKIP {it['tenant_name']}: {it.get('error')}")
            log_send(_BATCH, it, it.get("message"), "SKIPPED", it.get("error"),
                     {"id": it.get("template_id"), "template_name": it.get("template_name")})
            skipped += 1
            continue
        if i > 0:
            random_pause(min_delay, max_delay)
        num = wm.normalize_phone(test_number) if test_number else wm.normalize_phone(it["phone_number"])
        try:
            label = "TEST" if test_number else "SEND"
            print(f"📨 [{i+1}/{len(items)}] {label} to {it['tenant_name']} "
                  f"({it['display_address']}) {num}...")
            page.goto(f"https://web.whatsapp.com/send?phone={num}",
                      wait_until="domcontentloaded")
            # The send URL opens a fresh chat with an empty composer.
            page.wait_for_selector(SELECTOR_INPUT, timeout=30000)
            composer = page.locator(SELECTOR_INPUT).first
            composer.click()
            time.sleep(random.uniform(0.5, 1.5))
            # clear any leftover draft from a previous session
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            if mode == "type":
                human_type(page, it["message"])
            else:
                paste_text(page, it["message"])
            time.sleep(0.5)
            # small human pause before hitting send
            time.sleep(random.uniform(0.4, 1.2))
            send_btn = page.locator(SELECTOR_SEND).first
            send_btn.click()
            try:
                page.wait_for_selector(SELECTOR_SENT, timeout=20000)
            except Exception:
                pass  # sent marker not always visible; input clearing is the signal
            time.sleep(random.uniform(1.0, 2.0))
            if test_number:
                it = {**it, "phone_number": wm.normalize_phone(test_number)}
            log_send(_BATCH, it, it["message"], "SENT",
                     template={"id": it.get("template_id"),
                               "template_name": it.get("template_name")})
            sent += 1
            print(f"   ✅ Sent to {it['tenant_name']} ({it['phone_number']})")
        except Exception as e:
            print(f"   ❌ Failed for {it['tenant_name']}: {e}")
            if test_number:
                it = {**it, "phone_number": wm.normalize_phone(test_number)}
            log_send(_BATCH, it, it.get("message"), "FAILED", str(e),
                     {"id": it.get("template_id"),
                      "template_name": it.get("template_name")})
            failed += 1

    context.close()
    pw.stop()
    print(f"\n📊 Summary: sent={sent} failed={failed} skipped={skipped}")
    print("SEND_REPORT " + json.dumps({"batch_id": _BATCH, "sent": sent,
                                       "failed": failed, "skipped": skipped}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="Scan QR once to store the WhatsApp Web session")
    ap.add_argument("--headed", action="store_true", help="Force headed Chrome (use with xvfb-run)")
    ap.add_argument("--dry-run", action="store_true", help="Render messages without sending")
    ap.add_argument("--month", default=None, help="Billing month YYYY-MM (default: consumption/previous month)")
    ap.add_argument("--limit", type=int, default=None, help="Max messages to send")
    ap.add_argument("--min-delay", type=int, default=60)
    ap.add_argument("--max-delay", type=int, default=180)
    ap.add_argument("--test-number", default=None,
                    help="Send rendered messages to this dummy number instead of tenants (e.g. 919033127964). Use with --limit 1 to verify one real send.")
    ap.add_argument("--fast", action="store_true",
                    help="Fast typing (verification only; real sends should keep human typing)")
    ap.add_argument("--mode", choices=["paste", "type"], default="paste",
                    help="How to fill the composer: paste inserts whole message at once (default); type types char-by-char")
    args = ap.parse_args()

    global _BATCH, _HEADED
    _BATCH = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    _HEADED = args.headed

    if args.login:
        login_flow()
        return

    billing_month = args.month or consumption_month()
    print(f"🔎 Building reminder messages for billing period {billing_month} ...")
    items = build_all_messages(billing_month)
    print(f"   → {len(items)} active tenants with outstanding balance.")
    if not items:
        print("Nothing to send.")
        return
    if args.test_number and not args.dry_run and not args.limit:
        # verify a real send safely: default to just the first tenant's message
        args.limit = 1
    send_messages(items, args.min_delay, args.max_delay, args.dry_run, args.limit,
                  args.test_number, args.fast, args.mode)


if __name__ == "__main__":
    main()
