import os
import sys
import base64
import requests
import logging
import csv
import json
import re
import time
import random
import argparse
import uuid
from datetime import datetime
from pathlib import Path
from db_config import get_db_connection

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / ".torrent_state.json"
LOCK_PATH = BASE_DIR / ".torrent_scraper.lock"

# --- CONFIGURATION ---

logging.basicConfig(filename='automation_engine.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def _state_write(st):
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)

def _state_init(kind="torrent", execution_month=None):
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
            st.setdefault("lines", []).append({"ts": datetime.now().strftime("%H:%M:%S"), "line": str(line)[:350]})
            st["lines"] = st["lines"][-200:]
            st["updated_at"] = datetime.now().isoformat()
            _state_write(st)
        except Exception:
            pass

class TorrentEngine:
    def __init__(self, access_token):
        self.session = requests.Session()
        self.base_url = "https://connect.torrentpower.com"
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def get_amount(self, enc_service):
        url = f"{self.base_url}/payment/payment/getamount"
        # Using the pre-encrypted value directly from database token mapping records
        return self.session.post(url, json={"encryptFlag":"1", "encrypt": enc_service}).json()

def determine_bill_type(last_pay_str):
    if not last_pay_str: return "NORMAL"
    match = re.search(r'/Date\((\d+)\)/', last_pay_str)
    
    if not match: return "NORMAL"
    pay_date = datetime.fromtimestamp(int(match.group(1)) / 1000.0)
    print(f"Parsed last payment date: {pay_date}")
    now = datetime.now()
    start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_of_last_month = (start_of_this_month.replace(month=start_of_this_month.month - 1) 
                           if start_of_this_month.month > 1 else start_of_this_month.replace(year=start_of_this_month.year-1, month=12))
    return "NOTICE" if pay_date < start_of_last_month else "NORMAL"

def parse_ms_date(value):
    """Parses the .NET /Date(ms)/ format used by torrent get_amount fields."""
    if not value:
        return None
    match = re.search(r'/Date\((\d+)\)/', str(value))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)) / 1000.0)

def compute_consumption_month(fetch_date):
    """Postpaid billing: a bill fetched in month M is the PREVIOUS month's
    consumption bill. Returns the consumption month as 'YYYY-MM'."""
    year, month = fetch_date.year, fetch_date.month
    if month == 1:
        year -= 1
        month = 12
    else:
        month -= 1
    return f"{year}-{month:02d}"

def parse_get_amount_response(data, fetch_date=None):
    """Extracts bill fields from the torrent get_amount 'd' payload.

    execution_month is set to the CONSUMPTION month (fetch month - 1) because
    torrent bills are postpaid. last_bill_amt / last_billed_units / last_bill_date
    are stored for DISPLAY ONLY and must never be used in any calculation."""
    if fetch_date is None:
        fetch_date = datetime.now()
    amt = data.get("OsAmt")
    partner = data.get("Partner")
    last_pay = data.get("LastPay")
    bill_type = determine_bill_type(last_pay)

    last_bill_date = parse_ms_date(data.get("LastBill"))
    last_bill_amt = data.get("LastBillAmt")
    last_billed_units = data.get("LastBilledUnits")

    return {
        "amount": amt,
        "partner": partner,
        "bill_type": bill_type,
        "last_pay_date": parse_ms_date(last_pay),
        "last_bill_date": last_bill_date,
        "last_bill_amt": last_bill_amt,
        "last_billed_units": last_billed_units,
        "execution_month": compute_consumption_month(fetch_date),
    }

def run_multi_account_pipeline(execution_month=None, limit=None, logger=print):
    if execution_month is None:
        execution_month = compute_consumption_month(datetime.now())
    conn = get_db_connection()
    cur = conn.cursor()
    # Validate month format
    try:
        datetime.strptime(execution_month, "%Y-%m")
    except ValueError:
        raise ValueError(f"Invalid execution_month {execution_month!r}, expected YYYY-MM")
    
    # Prepare monthly_results.csv and archive path
    csv_path = BASE_DIR / "monthly_results.csv"
    with open(csv_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Service', 'Partner', 'Amount', 'Type'])

        cur.execute("SELECT id, access_token FROM rental_core.torrent_accounts")
        accounts = cur.fetchall()
        logger(f"🔗 Found {len(accounts)} accounts to process for month {execution_month}.")
        if limit:
            accounts = accounts[:limit]
            logger(f"Limit applied — processing first {len(accounts)} account(s).")
        
        total_services = 0
        for acc_id, access_token in accounts:
            engine = TorrentEngine(access_token)
            logger(f"🚀 Processing account {acc_id} with access token {access_token[:10]}...")
            
            cur.execute("SELECT service_number, encrypted_value FROM rental_core.utility_metrics WHERE account_id = %s", (acc_id,))
            services = cur.fetchall()
            logger(f"📡 Processing {len(services)} services for account {acc_id}.")
            if limit and total_services >= limit:
                break
            
            for (service, encrypted_value) in services:
                if limit and total_services >= limit:
                    break
                total_services += 1
                if not encrypted_value:
                    logger(f"⚠️ Skipped: No pre-encrypted token value found in DB for Service {service}")
                    continue
                    
                try:
                    sleep_duration = random.uniform(2.0, 4.0)
                    logger(f"⏳ Pausing for {sleep_duration:.2f}s to protect API rate limits...")
                    time.sleep(sleep_duration)

                    logger(f"🔒 Utilizing persistent DB token for service {service}: {encrypted_value[:20]}...")
                    
                    resp = engine.get_amount(encrypted_value)
                    data = resp.get("d", {})
                    
                    parsed = parse_get_amount_response(data)
                    amt = parsed["amount"]
                    partner = parsed["partner"]
                    bill_type = parsed["bill_type"]
                    pay_date = parsed["last_pay_date"]
                    last_bill_date = parsed["last_bill_date"]
                    last_bill_amt = parsed["last_bill_amt"]
                    last_billed_units = parsed["last_billed_units"]

                    logger(f"Service: {service}, Amount: {amt}, Partner: {partner}, Type: {bill_type}, Last Payment: {pay_date}, LastBillDate: {last_bill_date}, LastBillAmt: {last_bill_amt}, LastBilledUnits: {last_billed_units}")
                    
                    cur.execute("""
                        INSERT INTO rental_core.torrent_bill_master 
                            (service_number, amount, partner_name, bill_type, last_pay_date,
                             last_bill_date, last_bill_amt, last_billed_units, execution_month, created_at)
                        VALUES 
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (service_number, execution_month) 
                        DO UPDATE SET 
                            amount = EXCLUDED.amount,
                            partner_name = EXCLUDED.partner_name,
                            bill_type = EXCLUDED.bill_type,
                            last_pay_date = EXCLUDED.last_pay_date,
                            last_bill_date = EXCLUDED.last_bill_date,
                            last_bill_amt = EXCLUDED.last_bill_amt,
                            last_billed_units = EXCLUDED.last_billed_units,
                            created_at = NOW()
                    """, (service, amt, partner, bill_type, pay_date,
                          last_bill_date, last_bill_amt, last_billed_units, execution_month))
                    
                    writer.writerow([service, partner, amt, bill_type])
                    
                except Exception as e:
                    logging.error(f"Pipeline failed for service {service}: {e}")
                    logger(f"❌ Error for service {service}: {e}")
                    conn.rollback()

    conn.commit()
    cur.close()
    conn.close()
    logger(f"✅ Master bills for {execution_month} populated and upserted successfully.")
    # Archive monthly_results.csv per month
    try:
        import shutil
        arch_dir = BASE_DIR / "archive" / "torrent_reports"
        arch_dir.mkdir(parents=True, exist_ok=True)
        tag = execution_month.replace("-", "")
        shutil.copy2(csv_path, arch_dir / f"torrent_report_{tag}.csv")
        # Also copy to monthly_results_{tag}.csv for compatibility
        shutil.copy2(csv_path, BASE_DIR / f"monthly_results_{tag}.csv")
        logger(f"Archived torrent report to archive/torrent_reports/torrent_report_{tag}.csv")
    except Exception as ae:
        logger(f"Archive warning: {ae}")
    return execution_month

def main():
    parser = argparse.ArgumentParser(description="Torrent Power scraper — current-month aware")
    parser.add_argument("--month", default=None, help="Execution month YYYY-MM (consumption month). Default: previous calendar month")
    parser.add_argument("--dry-run", action="store_true", help="Dry run — generate fake data without calling API")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N accounts/services (debug)")
    parser.add_argument("--headed", action="store_true", help="Unused, kept for parity with gujgas scraper")
    args = parser.parse_args()

    execution_month = args.month or compute_consumption_month(datetime.now())
    try:
        datetime.strptime(execution_month, "%Y-%m")
    except ValueError:
        print(f"Invalid --month {execution_month!r}, expected YYYY-MM", file=sys.stderr)
        sys.exit(2)

    import fcntl
    lock_file = open(str(LOCK_PATH), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another Torrent scrape is already running (lock {LOCK_PATH}). Try again later.", file=sys.stderr)
        sys.exit(0)

    st = _state_init("torrent", execution_month=execution_month)
    tee = StateTee(sys.stdout, st["run_id"])
    orig_stdout = sys.stdout
    sys.stdout = tee
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
        if args.dry_run:
            log(f"🧪 DRY RUN — generating fake torrent data for {execution_month}")
            # Generate fake CSV without DB/API
            csv_path = BASE_DIR / "monthly_results.csv"
            with open(csv_path, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Service', 'Partner', 'Amount', 'Type'])
                writer.writerow(['500542469', 'Torrent', '400.00', 'NORMAL'])
                writer.writerow(['500542470', 'Torrent', '1730.00', 'NORMAL'])
            import shutil
            arch_dir = BASE_DIR / "archive" / "torrent_reports"
            arch_dir.mkdir(parents=True, exist_ok=True)
            tag = execution_month.replace("-", "")
            shutil.copy2(csv_path, arch_dir / f"torrent_report_{tag}.csv")
            log(f"DRY RUN archived to torrent_report_{tag}.csv")
            summary = {"execution_month": execution_month, "accounts": 2, "csv": str(csv_path)}
            print("TORRENT_SCRAPE_REPORT " + json.dumps(summary))
            _state_finish("finished", summary=summary, run_id=st["run_id"])
        else:
            log(f"🔎 Torrent scrape starting for execution month {execution_month} (consumption month)")
            run_multi_account_pipeline(execution_month=execution_month, limit=args.limit, logger=log)
            summary = {"execution_month": execution_month, "csv": str(BASE_DIR / "monthly_results.csv")}
            # Count bills for summary
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS cnt FROM rental_core.torrent_bill_master WHERE execution_month=%s", (execution_month,))
                cnt = cur.fetchone()["cnt"] if cur.rowcount else 0
                summary["bills"] = int(cnt or 0)
                cur.close(); conn.close()
            except Exception:
                pass
            print("TORRENT_SCRAPE_REPORT " + json.dumps(summary))
            _state_finish("finished", summary=summary, run_id=st["run_id"])
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Torrent scrape failed: {e}")
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