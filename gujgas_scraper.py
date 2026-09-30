#!/usr/bin/env python3
"""
Gujarat Gas Scraper — v2 (current-month aware, GUI-triggerable)

Scrapes https://iconnect.gujaratgas.com billing + payment history for every
customer account linked to the logged-in portal user, produces the same
CSV/XLSX artefacts as the original gujgas_fetch.py, and optionally imports
them into rental_core.gas_bill_master (delegates to import_gas_metrics.py).

Key improvements over the original gujgas_fetch.py:
  * Sync Playwright (consistent with send_due_reminders.py) — headless by
    default, --headed for debugging.
  * --month YYYY-MM : execution month (postpaid consumption month).  If
    omitted it defaults to the *consumption month* (previous calendar month),
    so a GUI click on 2026-09-04 scrapes 2026-08 — the same month that
    allocate_tenant_bills + monthly_ledgers target.
  * --no-import flag — GUI default is to scrape + import in one click.
  * Per-account isolation — one account failing never aborts the batch.
  * Live GUI state in .gujgas_state.json (polled by /api/gujgas/scraper-status).
  * File-lock (.gujgas_scraper.lock) prevents concurrent portal logins.
  * Structured JSON report on stdout: GAS_SCRAPE_REPORT { ... } for the API.

Usage:
  python gujgas_scraper.py [--month 2026-08] [--headed] [--no-import] [--dry-run] [--limit 2]
  xvfb-run -a python gujgas_scraper.py --month 2026-08   # server headless safety

GUI trigger:
  POST /api/operations/run-gujgas-scraper  (background task, see gui_api.py)
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

USERNAME = os.getenv("GUJGAS_USERNAME")
PASSWORD = os.getenv("GUJGAS_PASSWORD")

LOGIN_URL = "https://iconnect.gujaratgas.com/Portal/default.aspx"
BILLING_URL = "https://iconnect.gujaratgas.com/Portal/BillingHistory.aspx"

# ---------------------------------------------------------------------------
# Helpers (mirrored from gujgas_fetch.py for compatibility)
# ---------------------------------------------------------------------------

def consumption_month(dt=None):
    """Postpaid billing: bill generated this month is previous month's consumption."""
    dt = dt or datetime.now()
    if dt.month == 1:
        return f"{dt.year - 1}-12"
    return f"{dt.year}-{dt.month - 1:02d}"


def clean_amount(raw_val: str):
    if not raw_val or str(raw_val).strip() in ("", "N/A", "None"):
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw_val))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def clean_scm(raw_val: str):
    """Parse SCM reading like '4594.00 SCM' -> 4594.0"""
    if not raw_val or str(raw_val).strip() in ("", "N/A", "None"):
        return None
    m = re.search(r"([\d,]+\.\d+|[\d,]+)", str(raw_val))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def to_db_date(date_str: str):
    if not date_str or str(date_str).strip() in ("", "N/A"):
        return None
    try:
        dt = datetime.strptime(str(date_str).strip(), "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def calculate_billing_period(current_date_str: str, prev_date_str: str):
    try:
        cur_dt = datetime.strptime(current_date_str.strip(), "%d/%m/%Y")
        prev_dt = datetime.strptime(prev_date_str.strip(), "%d/%m/%Y")
        start_dt = prev_dt + timedelta(days=1)
        return {
            "period_str": f"{start_dt.strftime('%d/%m/%Y')} to {cur_dt.strftime('%d/%m/%Y')}",
            "period_from": start_dt.strftime("%Y-%m-%d"),
            "period_to": cur_dt.strftime("%Y-%m-%d"),
        }
    except Exception:
        iso_cur = to_db_date(current_date_str)
        return {
            "period_str": f"Up to {current_date_str}" if current_date_str else "",
            "period_from": None,
            "period_to": iso_cur,
        }


# ---------------------------------------------------------------------------
# GUI state (mirrors send_due_reminders .sender_state.json pattern)
# ---------------------------------------------------------------------------
STATE_PATH = BASE_DIR / ".gujgas_state.json"
LOCK_PATH = BASE_DIR / ".gujgas_scraper.lock"


def _state_write(st):
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


def _state_init(kind="gujgas", execution_month=None):
    st = {
        "run_id": uuid.uuid4().hex[:8],
        "kind": kind,
        "pid": os.getpid(),
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "lines": [],
        "summary": {"execution_month": execution_month} if execution_month else None,
        "execution_month": execution_month,
    }
    _state_write(st)
    return st


def _state_finish(status, summary=None, run_id=None):
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        if run_id and st.get("run_id") != run_id:
            return
        st["status"] = status
        st["finished_at"] = datetime.now().isoformat()
        st["updated_at"] = st["finished_at"]
        if summary is not None:
            st["summary"] = summary
        _state_write(st)
    except Exception:
        pass


class StateTee:
    """Tees prints into .gujgas_state.json for live GUI polling."""
    def __init__(self, orig, run_id):
        self._orig = orig
        self.run_id = run_id

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
        if not line.strip():
            return
        try:
            with open(STATE_PATH) as f:
                st = json.load(f)
            if st.get("run_id") != self.run_id:
                return
            st.setdefault("lines", []).append(
                {"ts": datetime.now().strftime("%H:%M:%S"), "line": str(line)[:350]}
            )
            st["lines"] = st["lines"][-200:]
            st["updated_at"] = datetime.now().isoformat()
            _state_write(st)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Core scraping (sync Playwright)
# ---------------------------------------------------------------------------

def scrape_gujgas(execution_month=None, headed=False, dry_run=False, limit=None, logger=print):
    """
    Returns (raw_records, csv_path, xlsx_path) and writes files to BASE_DIR.
    execution_month is YYYY-MM consumption month (previous calendar month).
    """
    if dry_run:
        logger("🧪 DRY RUN — generating 2 fake records without portal login")
        fake = [
            {
                "account_number": "500000832931",
                "bp_number": "2000862677",
                "customer_name": "UTTAMBHAI RAMUBHAI PATEL (500000832931)",
                "latest_invoice_no": "R103101536423",
                "invoice_date_raw": "03/06/2026",
                "period_str": "22/03/2026 to 03/06/2026",
                "period_from": "2026-03-22",
                "period_to": "2026-06-03",
                "billed_amount_raw": "1840.00",
                "due_date_raw": "18/06/2026",
                "paid_date_raw": "16/06/2026",
                "paid_amount_raw": "1840.00",
            },
            {
                "account_number": "500001290775",
                "bp_number": "2001297742",
                "customer_name": "HANSABEN UTTAMBHAI PATEL (500001290775)",
                "latest_invoice_no": "R102301523817",
                "invoice_date_raw": "03/06/2026",
                "period_str": "22/03/2026 to 03/06/2026",
                "period_from": "2026-03-22",
                "period_to": "2026-06-03",
                "billed_amount_raw": "2673.00",
                "due_date_raw": "18/06/2026",
                "paid_date_raw": "16/06/2026",
                "paid_amount_raw": "2673.00",
            },
        ]
        raw_records = fake[:limit] if limit else fake
    else:
        if not USERNAME or not PASSWORD:
            raise RuntimeError(
                "Missing GUJGAS_USERNAME or GUJGAS_PASSWORD in .env. "
                "Add them: GUJGAS_USERNAME=<portal email>  GUJGAS_PASSWORD=<password>"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        raw_records = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = context.new_page()

            logger(f"Opening Login Page: {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)

            logger("Submitting login credentials...")
            page.fill("#txtLogin", USERNAME)
            pwd_input = page.locator("input[name='txtpwd']:not([style*='display: none'])").first
            if pwd_input.count() == 0:
                pwd_input = page.locator("#txtpwd, input[type='password']").first
            pwd_input.fill(PASSWORD)
            page.click("#btnlogin")

            logger("Waiting for session to initialise (#ddlAddress)...")
            try:
                page.wait_for_selector("#ddlAddress", timeout=60000)
            except Exception as e:
                try:
                    page.screenshot(path=str(BASE_DIR / "static" / "gujgas_login_failed.png"))
                except Exception:
                    pass
                raise RuntimeError(f"Login failed — #ddlAddress not found after 60s. Check credentials or portal change. ({e})")

            if "BillingHistory.aspx" not in page.url:
                logger("Navigating to Billing History...")
                page.goto(BILLING_URL, wait_until="networkidle", timeout=30000)
                page.wait_for_selector("#ddlAddress", timeout=30000)

            accounts = page.evaluate("""() => {
                const sel=document.getElementById('ddlAddress');
                if(!sel) return [];
                return Array.from(sel.options)
                    .filter(o=>o.value && o.value.trim()!=='')
                    .map(o=>({value:o.value, text:o.innerText.trim(),
                              accountNum:o.getAttribute('utilityaccountnum')||'',
                              bpNumber:o.getAttribute('bpnumber')||'',
                              zipcode:o.getAttribute('zipcode')||''}));
            }""")
            logger(f"Discovered {len(accounts)} registered connection(s).")
            if limit:
                accounts = accounts[:limit]
                logger(f"Limit applied — scraping first {len(accounts)} account(s).")

            for idx, acc in enumerate(accounts, 1):
                acc_id = acc.get("accountNum") or acc.get("text") or acc.get("value")
                try:
                    logger(f"[{idx}/{len(accounts)}] Processing: {acc.get('text')} ({acc_id})")

                    current_val = page.evaluate("() => document.getElementById('ddlAddress').value")
                    if current_val != acc["value"]:
                        logger("  Switching account dropdown...")
                        # Match gujgas_fetch.py: use expect_navigation with networkidle to ensure postback completes
                        try:
                            with page.expect_navigation(wait_until="networkidle", timeout=15000):
                                page.evaluate("""(val) => {
                                    const sel=document.getElementById('ddlAddress');
                                    sel.value=val;
                                    if(typeof __doPostBack==='function') __doPostBack('ctl00$ddlAddress','');
                                    else sel.dispatchEvent(new Event('change',{bubbles:true}));
                                }""", acc["value"])
                        except Exception:
                            time.sleep(2)
                    else:
                        logger("  Account already active, reading directly...")

                    bill_tab = page.locator("#billpaybillingDetails a")
                    if bill_tab.count() > 0:
                        bill_tab.click()
                        time.sleep(1)

                    try:
                        page.wait_for_selector("#tblBillingHistory tbody tr td:nth-child(2)", timeout=10000)
                    except Exception:
                        time.sleep(2)

                    # --- A. Bill Statements ---
                    bill_rows = page.locator("#tblBillingHistory tbody tr")
                    row_count = bill_rows.count()

                    latest_bill_no = ""
                    bill_date = ""
                    billed_amount_raw = ""
                    due_date = ""
                    prev_bill_date = ""

                    if row_count > 0:
                        row1 = bill_rows.nth(0)
                        tds = row1.locator("td")
                        if tds.count() >= 5:
                            latest_bill_no = tds.nth(1).inner_text().strip()
                            bill_date = tds.nth(2).inner_text().strip()
                            billed_amount_raw = tds.nth(3).inner_text().strip()
                            due_date = tds.nth(4).inner_text().strip()
                        if row_count >= 2:
                            row2 = bill_rows.nth(1)
                            tds2 = row2.locator("td")
                            if tds2.count() >= 3:
                                prev_bill_date = tds2.nth(2).inner_text().strip()

                    period_info = calculate_billing_period(bill_date, prev_bill_date)

                    # --- B. Payment History ---
                    paid_date = "N/A"
                    paid_amount_raw = ""
                    try:
                        pay_tab = page.locator("#billpaymentDetails a")
                        if pay_tab.count() > 0:
                            pay_tab.click()
                            try:
                                page.wait_for_selector("#tblPaymentHistory tbody tr td:nth-child(2)", timeout=6000)
                            except Exception:
                                time.sleep(1)
                            pay_rows = page.locator("#tblPaymentHistory tbody tr")
                            if pay_rows.count() > 0:
                                first_pay = pay_rows.nth(0)
                                pay_tds = first_pay.locator("td")
                                if pay_tds.count() >= 3:
                                    paid_date = pay_tds.nth(1).inner_text().strip()
                                    paid_amount_raw = pay_tds.nth(2).inner_text().strip()
                    except Exception as pe:
                        logger(f"  [Notice] Payment history skipped: {pe}")

                    # --- C. Meter Reading History (USAGE -> MeterReadingHistory.aspx, table#table1) ---
                    # Probe-verified 2026-09-14: table#table1 header Reading Date | Reading Reason | Meter Reading (e.g. 3/09/2026 4594.00 SCM)
                    # Store only latest reading; consumed_units = latest - previous if >=2 rows else None (single row = first reading per spec)
                    meter_reading_latest_raw = ""
                    meter_reading_previous_raw = ""
                    reading_date_latest_raw = ""
                    reading_date_previous_raw = ""
                    reading_reason_latest_raw = ""
                    consumed_units_raw = None
                    try:
                        page.goto("https://iconnect.gujaratgas.com/Portal/MeterReadingHistory.aspx", wait_until="networkidle", timeout=20000)
                        try:
                            page.wait_for_selector("#table1", timeout=8000)
                        except Exception:
                            time.sleep(1)
                        # Check header present
                        has_header = page.evaluate("()=> (document.querySelector('#table1')?.innerText||'').toUpperCase().includes('READING DATE')")
                        if has_header:
                            rows = page.evaluate("""() => {
                                const t=document.querySelector('#table1');
                                return Array.from(t.querySelectorAll('tbody tr')).map(tr=>Array.from(tr.querySelectorAll('td')).map(td=>td.innerText.trim()));
                            }""")
                            py_parsed=[]
                            for r in rows:
                                if len(r)>=3 and r[0] and r[2]:
                                    # r = [Reading Date, Reading Reason, Meter Reading]
                                    reading_val = clean_scm(r[2])
                                    if reading_val is not None:
                                        py_parsed.append({"reading_date_raw": r[0], "reading_reason": r[1], "meter_raw": r[2], "val": reading_val})
                            if py_parsed:
                                meter_reading_latest_raw = py_parsed[0]["meter_raw"]
                                reading_date_latest_raw = py_parsed[0]["reading_date_raw"]
                                reading_reason_latest_raw = py_parsed[0]["reading_reason"]
                                if len(py_parsed) >= 2:
                                    meter_reading_previous_raw = py_parsed[1]["meter_raw"]
                                    reading_date_previous_raw = py_parsed[1]["reading_date_raw"]
                                    try:
                                        consumed_units_raw = round(py_parsed[0]["val"] - py_parsed[1]["val"], 2)
                                    except Exception:
                                        consumed_units_raw = None
                                elif len(py_parsed) == 1:
                                    # Single data row => first reading for this service (per user: "if only one raw means its the 2st reading")
                                    # For first-time bill, consumed = latest reading (previous is 0 as per meter start)
                                    try:
                                        consumed_units_raw = round(py_parsed[0]["val"], 2)
                                    except Exception:
                                        consumed_units_raw = None
                                else:
                                    consumed_units_raw = None
                            # Debug log
                            if py_parsed:
                                logger(f"  -> Meter: {reading_date_latest_raw} {meter_reading_latest_raw} -> consumed {consumed_units_raw if consumed_units_raw is not None else '—'} SCM ({len(py_parsed)} readings)")
                            else:
                                logger(f"  -> Meter: no readings (header only)")
                        else:
                            logger(f"  [Notice] Meter table not found for {acc_id}")
                        # Navigate back to BillingHistory for next account's billing (optional, next iteration will switch via ddlAddress)
                        try:
                            page.goto(BILLING_URL, wait_until="networkidle", timeout=15000)
                            page.wait_for_selector("#ddlAddress", timeout=10000)
                        except Exception:
                            pass
                    except Exception as me:
                        logger(f"  [Notice] Meter history skipped for {acc_id}: {me}")
                        # Try to return to billing page for next iteration
                        try:
                            page.goto(BILLING_URL, wait_until="networkidle", timeout=10000)
                        except Exception:
                            pass

                    raw_records.append({
                        "account_number": acc.get("accountNum", ""),
                        "bp_number": acc.get("bpNumber", ""),
                        "customer_name": acc.get("text", ""),
                        "latest_invoice_no": latest_bill_no,
                        "invoice_date_raw": bill_date,
                        "period_str": period_info["period_str"],
                        "period_from": period_info["period_from"],
                        "period_to": period_info["period_to"],
                        "billed_amount_raw": billed_amount_raw,
                        "due_date_raw": due_date,
                        "paid_date_raw": paid_date,
                        "paid_amount_raw": paid_amount_raw,
                        "meter_reading_latest_raw": meter_reading_latest_raw,
                        "meter_reading_previous_raw": meter_reading_previous_raw,
                        "reading_date_latest_raw": reading_date_latest_raw,
                        "reading_date_previous_raw": reading_date_previous_raw,
                        "reading_reason_latest_raw": reading_reason_latest_raw,
                        "consumed_units_raw": consumed_units_raw,
                    })
                    logger(f"  -> Period: {period_info['period_str']} | Billed: {billed_amount_raw} | Paid: {paid_amount_raw} | Consumed: {consumed_units_raw if consumed_units_raw is not None else '—'} SCM")

                except Exception as e:
                    logger(f"  ❌ Failed for account {acc_id}: {e} — continuing to next")
                    raw_records.append({
                        "account_number": acc.get("accountNum", ""),
                        "bp_number": acc.get("bpNumber", ""),
                        "customer_name": acc.get("text", ""),
                        "latest_invoice_no": "",
                        "invoice_date_raw": "",
                        "period_str": "",
                        "period_from": None,
                        "period_to": None,
                        "billed_amount_raw": "",
                        "due_date_raw": "",
                        "paid_date_raw": "N/A",
                        "paid_amount_raw": "",
                        "meter_reading_latest_raw": "",
                        "meter_reading_previous_raw": "",
                        "reading_date_latest_raw": "",
                        "reading_date_previous_raw": "",
                        "reading_reason_latest_raw": "",
                        "consumed_units_raw": None,
                    })
                    continue

            browser.close()

    # ----------------- 4. Prepare DataFrames & write files -----------------
    # Enrich with plot/flat/tenant for report downloads (via gas_metrics -> rooms -> tenants/property)
    enrich_map = {}
    try:
        from db_config import get_db_connection as _gdb
        _conn = _gdb(as_dict=True)
        _cur = _conn.cursor()
        _cur.execute("""
            SELECT gm.customer_number, r.display_address AS flat_no, r.flat_number AS flat, pm.plot_number AS plot,
                   t.tenant_name
            FROM rental_core.gas_metrics gm
            LEFT JOIN rental_core.rooms r ON gm.room_id = r.id
            LEFT JOIN rental_core.property_masters pm ON r.master_id = pm.id
            LEFT JOIN rental_core.tenants t ON t.room_id = r.id AND t.is_active='Y'
        """)
        for row in _cur.fetchall():
            enrich_map[row["customer_number"]] = {
                "plot": row["plot"] or "",
                "flat": row["flat"] or "",
                "tenant_name": row["tenant_name"] or "",
                "flat_no": row["flat_no"] or "",
            }
        _cur.close(); _conn.close()
    except Exception as e:
        logger(f"Enrich warning: {e}")
        enrich_map = {}

    db_rows = [
        {
            "plot": enrich_map.get(r["account_number"], {}).get("plot", ""),
            "flat": enrich_map.get(r["account_number"], {}).get("flat", ""),
            "tenant_name": enrich_map.get(r["account_number"], {}).get("tenant_name", ""),
            "flat_no": enrich_map.get(r["account_number"], {}).get("flat_no", ""),
            "account_number": r["account_number"],
            "bp_number": r["bp_number"],
            "customer_name": r["customer_name"],
            "latest_invoice_no": r["latest_invoice_no"],
            "invoice_date": to_db_date(r["invoice_date_raw"]),
            "billing_period": r["period_str"],
            "period_from": r["period_from"],
            "period_to": r["period_to"],
            "billed_amount": clean_amount(r["billed_amount_raw"]),
            "invoice_due_date": to_db_date(r["due_date_raw"]),
            "previous_paid_date": to_db_date(r["paid_date_raw"]),
            "previous_paid_amount": clean_amount(r["paid_amount_raw"]),
            "meter_reading_latest": clean_scm(r.get("meter_reading_latest_raw", "")),
            "meter_reading_previous": clean_scm(r.get("meter_reading_previous_raw", "")),
            "reading_date_latest": to_db_date(r.get("reading_date_latest_raw", "")),
            "reading_date_previous": to_db_date(r.get("reading_date_previous_raw", "")),
            "reading_reason_latest": r.get("reading_reason_latest_raw", ""),
            "consumed_units": r.get("consumed_units_raw"),
        }
        for r in raw_records
    ]
    df_csv = pd.DataFrame(db_rows)
    csv_path = BASE_DIR / "gujarat_gas_report.csv"
    df_csv.to_csv(csv_path, index=False)

    excel_rows = [
        {
            "Plot": enrich_map.get(r["account_number"], {}).get("plot", ""),
            "Flat": enrich_map.get(r["account_number"], {}).get("flat", ""),
            "Tenant Name": enrich_map.get(r["account_number"], {}).get("tenant_name", ""),
            "Flat No": enrich_map.get(r["account_number"], {}).get("flat_no", ""),
            "Account Number": r["account_number"],
            "BP Number": r["bp_number"],
            "Customer Name": r["customer_name"],
            "Latest Invoice No": r["latest_invoice_no"],
            "Invoice Date": r["invoice_date_raw"],
            "Billing Period": r["period_str"],
            "Billed Amount (₹)": clean_amount(r["billed_amount_raw"]),
            "Invoice Due Date": r["due_date_raw"],
            "Previous Paid Date": r["paid_date_raw"],
            "Previous Paid Amount (₹)": clean_amount(r["paid_amount_raw"]),
            "Meter Reading Latest (SCM)": clean_scm(r.get("meter_reading_latest_raw", "")),
            "Meter Reading Previous (SCM)": clean_scm(r.get("meter_reading_previous_raw", "")),
            "Reading Date Latest": r.get("reading_date_latest_raw", ""),
            "Consumed Units (SCM)": r.get("consumed_units_raw"),
        }
        for r in raw_records
    ]
    xlsx_path = BASE_DIR / "gujarat_gas_report.xlsx"
    try:
        df_excel = pd.DataFrame(excel_rows)
        df_excel.to_excel(xlsx_path, index=False)
    except ModuleNotFoundError as e:
        if "openpyxl" in str(e):
            logger(f"⚠️  openpyxl not installed — skipping XLSX generation (CSV is ready). Install with: pip install openpyxl")
            xlsx_path = str(BASE_DIR / "gujarat_gas_report.xlsx")  # path even if not written
            # create empty placeholder so archive step doesn't fail
        else:
            raise

    # Archive copies with month suffix (e.g. gujarat_gas_report_202608.csv)
    try:
        arch_dir = BASE_DIR / "archive" / "gas_reports"
        arch_dir.mkdir(parents=True, exist_ok=True)
        tag = (execution_month or consumption_month()).replace("-", "")
        import shutil
        shutil.copy2(csv_path, arch_dir / f"gujarat_gas_report_{tag}.csv")
        shutil.copy2(xlsx_path, arch_dir / f"gujarat_gas_report_{tag}.xlsx")
        logger(f"Archived copies to archive/gas_reports/ with tag {tag}")
    except Exception as ae:
        logger(f"Archive warning: {ae}")

    logger("=" * 60)
    logger(f"Completed extraction for {len(raw_records)} account(s).")
    logger(f"1. Database CSV : {csv_path}")
    logger(f"2. Human Excel : {xlsx_path}")
    logger("=" * 60)

    return raw_records, str(csv_path), str(xlsx_path)


def main():
    parser = argparse.ArgumentParser(description="Gujarat Gas scraper — current-month aware")
    parser.add_argument("--month", default=None, help="Execution month YYYY-MM (consumption month). Default: previous calendar month")
    parser.add_argument("--headed", action="store_true", help="Run headed (debug) instead of headless")
    parser.add_argument("--no-import", action="store_true", help="Do not auto-import CSV into DB after scrape")
    parser.add_argument("--dry-run", action="store_true", help="Generate fake CSV/XLSX without portal login")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N accounts (debug)")
    args = parser.parse_args()

    execution_month = args.month or consumption_month()
    # validate month format
    try:
        datetime.strptime(execution_month, "%Y-%m")
    except ValueError:
        print(f"Invalid --month {execution_month!r}, expected YYYY-MM", file=sys.stderr)
        sys.exit(2)

    # File lock — prevent concurrent portal logins
    import fcntl
    lock_file = open(str(LOCK_PATH), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another Gujarat Gas scrape is already running (lock {LOCK_PATH}). Try again later.", file=sys.stderr)
        sys.exit(0)

    # State + tee for GUI live log
    st = _state_init("gujgas", execution_month=execution_month)
    tee = StateTee(sys.stdout, st["run_id"])
    orig_stdout = sys.stdout
    sys.stdout = tee

    # Wrap print for state — single log path via tee.log_line
    import builtins
    raw_print = builtins.print
    def print(*a, **k):  # noqa: A001
        raw_print(*a, **k)
        try:
            tee.log_line(" ".join(str(x) for x in a))
        except Exception:
            pass

    def log(msg):
        print(msg)

    try:
        log(f"🔎 Gujarat Gas scrape starting for execution month {execution_month} (consumption month)")
        raw_records, csv_path, xlsx_path = scrape_gujgas(
            execution_month=execution_month,
            headed=args.headed,
            dry_run=args.dry_run,
            limit=args.limit,
            logger=log,
        )

        # Auto-import unless disabled
        import_result = None
        if not args.no_import and not args.dry_run:
            try:
                log("📥 Importing CSV into rental_core.gas_bill_master ...")
                # Run importer as subprocess so its own prints are captured; also do inline for reliability
                import subprocess
                res = subprocess.run([sys.executable, str(BASE_DIR / "import_gas_metrics.py")],
                                     capture_output=True, text=True, timeout=120)
                log(res.stdout or "")
                if res.stderr:
                    log(res.stderr)
                import_result = {"returncode": res.returncode}
                if res.returncode != 0:
                    log(f"⚠️  import_gas_metrics.py exited {res.returncode}")
            except Exception as ie:
                log(f"⚠️  Import step failed: {ie}")

        summary = {
            "execution_month": execution_month,
            "accounts": len(raw_records),
            "csv": csv_path,
            "xlsx": xlsx_path,
            "import": import_result,
        }
        print("GAS_SCRAPE_REPORT " + json.dumps(summary))
        _state_finish("finished", summary=summary, run_id=st["run_id"])
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Scrape failed: {e}")
        _state_finish("failed", summary={"error": str(e), "execution_month": execution_month}, run_id=st["run_id"])
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass
        sys.exit(1)
    finally:
        sys.stdout = orig_stdout
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
