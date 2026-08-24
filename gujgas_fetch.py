import asyncio
import os
import re
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ----------------- LOAD ENVIRONMENT VARIABLES -----------------
load_dotenv()

USERNAME = os.getenv("GUJGAS_USERNAME")
PASSWORD = os.getenv("GUJGAS_PASSWORD")

if not USERNAME or not PASSWORD:
    raise ValueError("Missing GUJGAS_USERNAME or GUJGAS_PASSWORD in .env file.")

LOGIN_URL = "https://iconnect.gujaratgas.com/Portal/default.aspx"
BILLING_URL = "https://iconnect.gujaratgas.com/Portal/BillingHistory.aspx"
# --------------------------------------------------------------

# ----------------- FORMATTING & CLEANING HELPERS -----------------
def clean_amount(raw_val: str):
    """Strips currency symbols, commas, and spaces -> returns float or None."""
    if not raw_val or raw_val == "N/A":
        return None
    cleaned = re.sub(r"[^\d.]", "", raw_val)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

def to_db_date(date_str: str):
    """Converts DD/MM/YYYY to ISO standard YYYY-MM-DD for PostgreSQL."""
    if not date_str or date_str == "N/A":
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

def calculate_billing_period(current_date_str: str, prev_date_str: str):
    """Computes billing period: (Day after previous invoice date) to (Current invoice date)."""
    try:
        cur_dt = datetime.strptime(current_date_str.strip(), "%d/%m/%Y")
        prev_dt = datetime.strptime(prev_date_str.strip(), "%d/%m/%Y")
        start_dt = prev_dt + timedelta(days=1)
        return {
            "period_str": f"{start_dt.strftime('%d/%m/%Y')} to {cur_dt.strftime('%d/%m/%Y')}",
            "period_from": start_dt.strftime("%Y-%m-%d"),
            "period_to": cur_dt.strftime("%Y-%m-%d")
        }
    except Exception:
        iso_cur = to_db_date(current_date_str)
        return {
            "period_str": f"Up to {current_date_str}" if current_date_str else "",
            "period_from": None,
            "period_to": iso_cur
        }
# -----------------------------------------------------------------

async def run_automation():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await context.new_page()

        print(f"Opening Login Page: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="networkidle")

        # 1. Login
        print("Submitting login credentials...")
        await page.fill("#txtLogin", USERNAME)
        pwd_input = page.locator("input[name='txtpwd']:not([style*='display: none'])").first
        await pwd_input.fill(PASSWORD)
        await page.click("#btnlogin")

        print("Waiting for session to initialize...")
        await page.wait_for_selector("#ddlAddress", timeout=60000)

        # 2. Navigate to Billing History
        if "BillingHistory.aspx" not in page.url:
            print("Navigating to Billing History...")
            await page.goto(BILLING_URL, wait_until="networkidle")
            await page.wait_for_selector("#ddlAddress", timeout=30000)

        # 3. Extract accounts list
        accounts = await page.evaluate("""
            () => {
                const select = document.getElementById('ddlAddress');
                if (!select) return [];
                return Array.from(select.options)
                    .filter(opt => opt.value && opt.value.trim() !== '')
                    .map(opt => ({
                        value: opt.value,
                        text: opt.innerText.trim(),
                        accountNum: opt.getAttribute('utilityaccountnum') || '',
                        bpNumber: opt.getAttribute('bpnumber') || '',
                        zipcode: opt.getAttribute('zipcode') || ''
                    }));
            }
        """)

        print(f"\nDiscovered {len(accounts)} registered connections.")
        raw_records = []
        # --- FOR 1 ACCOUNT DEBUGGING ---
        #accounts = accounts[:1]
        #print(f"Debugging Mode: Running for -> {accounts[0]['text']}\n")

        all_records = []

        for idx, acc in enumerate(accounts, 1):
            acc_id = acc["accountNum"] or acc["text"]
            print(f"[{idx}/{len(accounts)}] Processing Account: {acc['text']} (Account: {acc_id})")

            # Check if this account is already the selected value in DOM
            current_selected_val = await page.evaluate("() => document.getElementById('ddlAddress').value")

            if current_selected_val != acc["value"]:
                print("  Switching account dropdown...")
                try:
                    async with page.expect_navigation(wait_until="networkidle", timeout=15000):
                        await page.evaluate(f"""
                            (val) => {{
                                const select = document.getElementById('ddlAddress');
                                select.value = val;
                                if (typeof __doPostBack === 'function') {{
                                    __doPostBack('ctl00$ddlAddress', '');
                                }} else {{
                                    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                }}
                            }}
                        """, acc["value"])
                except Exception:
                    await asyncio.sleep(2)
            else:
                print("  Account is already active, reading data directly...")

            # Ensure Bill Statements tab is selected
            bill_tab = page.locator("#billpaybillingDetails a")
            if await bill_tab.count() > 0:
                await bill_tab.click()
            
            # Explicitly wait for the data row to appear (Invoice Number starts with 'R' or has text)
            try:
                await page.wait_for_selector("#tblBillingHistory tbody tr td:nth-child(2)", timeout=10000)
            except Exception:
                await asyncio.sleep(2)

            # --- A. SCRAPE BILL STATEMENTS ---
            bill_rows = page.locator("#tblBillingHistory tbody tr")
            row_count = await bill_rows.count()

            latest_bill_no = ""
            bill_date = ""
            billed_amount_raw = ""
            due_date = ""
            prev_bill_date = ""

            if row_count > 0:
                # Row 1 (Latest Bill)
                row1 = bill_rows.nth(0)
                tds_row1 = row1.locator("td")
                
                if await tds_row1.count() >= 5:
                    latest_bill_no = (await tds_row1.nth(1).inner_text()).strip()
                    bill_date = (await tds_row1.nth(2).inner_text()).strip()
                    billed_amount_raw = (await tds_row1.nth(3).inner_text()).strip()
                    due_date = (await tds_row1.nth(4).inner_text()).strip()

                # Row 2 (Previous Bill for Period calculation)
                if row_count >= 2:
                    row2 = bill_rows.nth(1)
                    tds_row2 = row2.locator("td")
                    if await tds_row2.count() >= 3:
                        prev_bill_date = (await tds_row2.nth(2).inner_text()).strip()

            period_info = calculate_billing_period(bill_date, prev_bill_date)
            billing_period = f"Up to {bill_date}"

            # --- B. SCRAPE PAYMENT HISTORY ---
            paid_date = "N/A"
            paid_amount_raw = ""

            try:
                pay_tab = page.locator("#billpaymentDetails a")
                if await pay_tab.count() > 0:
                    await pay_tab.click()
                    
                    # Wait for payments table to load
                    try:
                        await page.wait_for_selector("#tblPaymentHistory tbody tr td:nth-child(2)", timeout=8000)
                    except Exception:
                        await asyncio.sleep(1.5)

                    pay_rows = page.locator("#tblPaymentHistory tbody tr")
                    if await pay_rows.count() > 0:
                        first_pay = pay_rows.nth(0)
                        pay_tds = first_pay.locator("td")
                        if await pay_tds.count() >= 3:
                            paid_date = (await pay_tds.nth(1).inner_text()).strip()
                            paid_amount_raw = (await pay_tds.nth(2).inner_text()).strip()
            except Exception as e:
                print(f"  [Notice] Payment history check skipped: {e}")

            # Collect raw data for transformation
            raw_records.append({
                "account_number": acc["accountNum"],
                "bp_number": acc["bpNumber"],
                "customer_name": acc["text"],
                "latest_invoice_no": latest_bill_no,
                "invoice_date_raw": bill_date,
                "period_str": period_info["period_str"],
                "period_from": period_info["period_from"],
                "period_to": period_info["period_to"],
                "billed_amount_raw": billed_amount_raw,
                "due_date_raw": due_date,
                "paid_date_raw": paid_date,
                "paid_amount_raw": paid_amount_raw,
            })

            print(f"  -> Period: {period_info['period_str']} | Billed: {billed_amount_raw} | Paid: {paid_amount_raw}")

        await browser.close()

        # ----------------- 4. PREPARE DATAFRAMES -----------------

        # --- CSV: Database-Ready (PostgreSQL Types) ---
        db_rows = [
            {
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
            }
            for r in raw_records
        ]

        df_csv = pd.DataFrame(db_rows)
        csv_file = "gujarat_gas_report.csv"
        df_csv.to_csv(csv_file, index=False)

        # --- EXCEL: Human-Readable Format ---
        excel_rows = [
            {
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
            }
            for r in raw_records
        ]

        df_excel = pd.DataFrame(excel_rows)
        excel_file = "gujarat_gas_report.xlsx"
        df_excel.to_excel(excel_file, index=False)

        print("\n" + "="*60)
        print(f"Completed extraction for {len(raw_records)} account(s).")
        print(f"1. Database CSV (PostgreSQL formatted): {csv_file}")
        print(f"2. Human-Readable Excel:               {excel_file}")
        print("="*60)

if __name__ == "__main__":
    asyncio.run(run_automation())