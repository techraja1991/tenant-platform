"""Test script for the postpaid bill-attribution changes in torrent_automation_engine.py.

IMPORTANT: This script NEVER calls the live torrent API and does NOT touch the
database. It validates the pure parsing/month-attribution logic against sample
get_amount payloads. Run with: ./venv/bin/python3 test_engine_postpaid.py
"""
import sys
from datetime import datetime
from torrent_automation_engine import (
    compute_consumption_month,
    parse_ms_date,
    determine_bill_type,
    parse_get_amount_response,
)

PASS = 0
FAIL = 0

def check(label, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  PASS  {label}: {actual}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}: got {actual!r}, expected {expected!r}")


# --- Sample get_amount payloads (simulating torrent responses) ---
# Executed in August 2026: fetched bill is July consumption.
AUG_FETCH_SAMPLE = {
    "d": {
        "OsAmt": 2110.00,
        "Partner": "53 / G1",
        "LastPay": "/Date(1747085400000)/",        # 2026-05-12 -> NOTICE
        "LastBill": "/Date(1785888000000)/",       # 2026-08-05 bill prepared in Aug (for July consumption)
        "LastBillAmt": 2110.00,
        "LastBilledUnits": 320,
    }
}

# Executed in July 2026: fetched bill is June consumption.
JUL_FETCH_SAMPLE = {
    "d": {
        "OsAmt": 1360.00,
        "Partner": "53 / G1",
        "LastPay": "/Date(1747085400000)/",
        "LastBill": "/Date(1783296000000)/",       # 2026-07-06 bill prepared in July (for June consumption)
        "LastBillAmt": 1360.00,
        "LastBilledUnits": 210,
    }
}

# Freshly paid service: OsAmt = 0 (this is why we must NOT re-run the live engine).
PAID_SAMPLE = {
    "d": {
        "OsAmt": 0.00,
        "Partner": "56 / 1",
        "LastPay": "/Date(1786032000000)/",        # 2026-08-07 -> NORMAL
        "LastBill": "/Date(1785888000000)/",
        "LastBillAmt": 230.00,
        "LastBilledUnits": 35,
    }
}


def test_compute_consumption_month():
    print("\n[compute_consumption_month]")
    check("Aug fetch -> Jul consumption", compute_consumption_month(datetime(2026, 8, 3)), "2026-07")
    check("Jul fetch -> Jun consumption", compute_consumption_month(datetime(2026, 7, 11)), "2026-06")
    check("Jan fetch -> prev Dec", compute_consumption_month(datetime(2026, 1, 15)), "2025-12")
    check("Dec fetch -> prev Nov", compute_consumption_month(datetime(2026, 12, 1)), "2026-11")


def test_parse_ms_date():
    print("\n[parse_ms_date]")
    check("null", parse_ms_date(None), None)
    check("ms epoch", parse_ms_date("/Date(1785888000000)/"), datetime.fromtimestamp(1785888000000 / 1000.0))
    check("no match", parse_ms_date("hello"), None)


def test_determine_bill_type():
    print("\n[determine_bill_type]")
    check("null -> NORMAL", determine_bill_type(None), "NORMAL")
    check("old pay -> NOTICE", determine_bill_type("/Date(1747085400000)/"), "NOTICE")
    check("recent pay -> NORMAL", determine_bill_type("/Date(1786032000000)/"), "NORMAL")


def test_parse_get_amount_response():
    print("\n[parse_get_amount_response - Aug fetch]")
    r = parse_get_amount_response(AUG_FETCH_SAMPLE["d"], fetch_date=datetime(2026, 8, 3))
    check("amount", r["amount"], 2110.00)
    check("partner", r["partner"], "53 / G1")
    check("bill_type", r["bill_type"], "NOTICE")
    check("execution_month = consumption month", r["execution_month"], "2026-07")
    check("last_bill_date", r["last_bill_date"], parse_ms_date("/Date(1785888000000)/"))
    check("last_bill_amt", r["last_bill_amt"], 2110.00)
    check("last_billed_units", r["last_billed_units"], 320)
    # CRITICAL: last_bill_amt must be stored but never used for month-light calc.
    # The allocated light bill for a NOTICE = amount - previous month amount (handled
    # by allocate_tenant_bills.py), NOT last_bill_amt.

    print("\n[parse_get_amount_response - Jul fetch]")
    r2 = parse_get_amount_response(JUL_FETCH_SAMPLE["d"], fetch_date=datetime(2026, 7, 11))
    check("execution_month", r2["execution_month"], "2026-06")

    print("\n[parse_get_amount_response - paid service]")
    r3 = parse_get_amount_response(PAID_SAMPLE["d"], fetch_date=datetime(2026, 8, 3))
    check("paid -> amount 0", r3["amount"], 0.00)
    check("paid -> NORMAL", r3["bill_type"], "NORMAL")
    check("execution_month", r3["execution_month"], "2026-07")


if __name__ == "__main__":
    test_compute_consumption_month()
    test_parse_ms_date()
    test_determine_bill_type()
    test_parse_get_amount_response()
    print(f"\n===== {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)
