#!/usr/bin/env bash
# Convenience wrapper for the WhatsApp reminder tools.
# Usage:
#   ./whatsapp_login.sh          # scan QR once (under xvfb for a real display)
#   ./whatsapp_send.sh [--dry-run] [--limit N] [--min-delay 60] [--max-delay 180]
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-$PWD/venv/bin/python}"
export PYTHONUNBUFFERED=1

case "${1:-}" in
  login)
    exec xvfb-run -a "$PY" send_whatsapp_reminders.py --login
    ;;
  send)
    shift
    exec xvfb-run -a "$PY" send_whatsapp_reminders.py "$@"
    ;;
  test)
    shift
    exec xvfb-run -a "$PY" send_whatsapp_reminders.py --test-number 919033127964 --fast "$@"
    ;;
  duedue)
    shift
    exec xvfb-run -a "$PY" send_due_reminders.py "$@"
    ;;
  duetest)
    shift
    exec xvfb-run -a "$PY" send_due_reminders.py --test-number 919033127964 --fast "$@"
    ;;
  *)
    echo "Usage: $0 login|send|test|duedue|duetest [--dry-run] [--limit N] [--min-delay S] [--max-delay S]"
    echo "  login    scan QR once (under xvfb)"
    echo "  send     send reminders to active tenants (paste-based)"
    echo "  test     send messages to dummy number 919033127964 for verification"
    echo "  duedue   send due-date reminders (NOTIFY/D1/DUE)"
    echo "  duetest  send due-date reminders to dummy number 919033127964"
    exit 1
    ;;
esac