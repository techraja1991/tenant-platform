#!/usr/bin/env python3
"""Daily due-date reminder scheduler sync (run at 00:00 each day).

Reads the admin-set master send time + enabled flag from rental_core.app_settings
and rewrites the systemd sender timer so it fires exactly once per day at that
time. This replaces the old every-5-minutes polling: the timer's OnCalendar is
kept in sync with whatever the admin configured in the GUI.

  * reminder_send_time    "HH:MM" -> tenant-due-reminders.timer OnCalendar=*-*-* HH:MM:00
  * reminder_send_enabled "N"     -> sender timer stopped + disabled (no daily run)
  * reminder_send_enabled "Y"     -> sender timer enabled + started at the right time

The sender timer activates tenant-due-reminders.service which runs
send_due_reminders.py (which itself skips tenants already paid).

Usage:
  python due_reminder_trigger.py [--sync]        # normal daily sync (default)
"""
import argparse
import os
import subprocess
import sys

from db_config import get_db_connection

SENDER_TIMER = "/etc/systemd/system/tenant-due-reminders.timer"
SYNC_TIMER = "tenant-due-reminders-sync.timer"
SENDER_TIMER_UNIT = "tenant-due-reminders.timer"
ENABLED_ATTR = "reminder_send_enabled"
TIME_ATTR = "reminder_send_time"


def _setting(key, default=None):
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM rental_core.app_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
        return row['value'] if row else default
    finally:
        cur.close()
        conn.close()


def _run(cmd):
    """Run a privileged command. The sync runs as root (its own systemd
    service has no User= directive), so no sudo is needed here."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"⚠️  {' '.join(cmd)} -> rc={res.returncode}: {res.stderr.strip()}")
        return False
    return True


def _timer_file(time_hhmm):
    return f"""[Unit]
Description=Tenant Platform - automated due-date WhatsApp reminders (master time {time_hhmm})

[Timer]
OnCalendar=*-*-* {time_hhmm}:00
Persistent=true

[Install]
WantedBy=timers.target
"""


def _read_existing_calendar():
    try:
        with open(SENDER_TIMER) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OnCalendar="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def sync():
    enabled = (_setting(ENABLED_ATTR, "Y") or "Y").upper()
    master = (_setting(TIME_ATTR, "10:00") or "10:00").strip()

    if enabled != "Y":
        print(f"⏸  reminder_send_enabled={enabled} — stopping + disabling sender timer.")
        _run(["systemctl", "stop", SENDER_TIMER_UNIT])
        _run(["systemctl", "disable", SENDER_TIMER_UNIT])
        return

    if len(master) != 5 or ":" not in master:
        print(f"⚠️  Invalid reminder_send_time={master!r}. Skipping.")
        return

    desired = f"*-*-* {master}:00"
    current = _read_existing_calendar()
    if current == desired:
        print(f"✅  Sender timer already scheduled at {master} — no change needed.")
        _run(["systemctl", "enable", SENDER_TIMER_UNIT])
        _run(["systemctl", "start", SENDER_TIMER_UNIT])
        return

    content = _timer_file(master)
    # write the new timer file (we run as root, so direct write is fine)
    with open(SENDER_TIMER, "w") as f:
        f.write(content)

    print(f"🔄  Sender timer rescheduled: {current or '(none)'} -> {desired}")
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", SENDER_TIMER_UNIT])
    _run(["systemctl", "start", SENDER_TIMER_UNIT])
    _run(["systemctl", "restart", SYNC_TIMER])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="Rewrite the sender timer (default action)")
    args = ap.parse_args()
    if args.sync or True:
        sync()


if __name__ == "__main__":
    main()