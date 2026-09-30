import os
import csv
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from db_config import get_db_connection

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / ".gas_import_state.json"
LOCK_PATH = BASE_DIR / ".gas_import.lock"

def _state_write(st):
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    import os as _os
    _os.replace(tmp, STATE_PATH)

def _state_init():
    st = {
        "run_id": uuid.uuid4().hex[:8],
        "kind": "gas_import",
        "pid": os.getpid(),
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "lines": [],
        "summary": None,
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

# --- CONFIGURATION ---
CSV_FILENAME = str(BASE_DIR / "gujarat_gas_report.csv")

def clean_field(val):
    """Removes outer double quotes, single quotes, and surrounding whitespace."""
    if not val:
        return ""
    s = val.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s

def parse_date(date_str):
    """Parse date from YYYY-MM-DD format."""
    if not date_str or date_str.strip() == "":
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None

def parse_float(val):
    """Parse float value from string."""
    if not val:
        return 0.0
    try:
        return float(str(val).strip())
    except ValueError:
        return 0.0

def process_gas_pipeline(_tee=None):
    # _tee is StateTee for GUI live log; print is already tee'd via StateTee+builtins.print, so just print
    def _log(msg):
        print(msg)
    if not os.path.exists(CSV_FILENAME):
        _log(f"❌ Aborted: Target source file '{CSV_FILENAME}' not found.")
        return

    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    
    # Postpaid billing: gas bills imported "this month" are actually the PREVIOUS
    # month's consumption to be collected, so default to the consumption month.
    now = datetime.now()
    execution_month = (f"{now.year - 1}-12" if now.month == 1 else f"{now.year}-{now.month - 1:02d}")
    print(f"🔄 Processing Gujarat Gas CSV import for cycle: {execution_month}...")
    print(f"📄 Source file: {CSV_FILENAME}")

    success_count = 0
    skipped_count = 0
    error_count = 0

    with open(CSV_FILENAME, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row_idx, row in enumerate(reader, start=1):
            try:
                # Extract and clean fields from the new CSV format
                account_number = clean_field(row.get("account_number", ""))
                bp_number = clean_field(row.get("bp_number", ""))
                customer_name = clean_field(row.get("customer_name", ""))
                latest_invoice_no = clean_field(row.get("latest_invoice_no", ""))
                invoice_date = parse_date(row.get("invoice_date", ""))
                billing_period = clean_field(row.get("billing_period", ""))
                period_from = parse_date(row.get("period_from", ""))
                period_to = parse_date(row.get("period_to", ""))
                billed_amount = parse_float(row.get("billed_amount", 0))
                invoice_due_date = parse_date(row.get("invoice_due_date", ""))
                previous_paid_date = parse_date(row.get("previous_paid_date", ""))
                previous_paid_amount = parse_float(row.get("previous_paid_amount", 0))
                
                # Extract customer_number - prefer account_number (gas_metrics uses account_number like 500000832931)
                # Fallback to bp_number if account not in gas_metrics, but try account first
                customer_number = account_number if account_number else bp_number
                # If account_number not in gas_metrics but bp_number is, use bp_number
                if customer_number:
                    cur.execute("SELECT 1 FROM rental_core.gas_metrics WHERE customer_number=%s LIMIT 1", (customer_number,))
                    if not cur.fetchone() and bp_number and bp_number != customer_number:
                        # Try bp_number as alternative
                        cur.execute("SELECT 1 FROM rental_core.gas_metrics WHERE customer_number=%s LIMIT 1", (bp_number,))
                        if cur.fetchone():
                            customer_number = bp_number
                
                if not customer_number:
                    print(f"⚠️ Row {row_idx}: Missing customer_number (bp_number/account_number). Skipping.")
                    skipped_count += 1
                    continue
                
                # Validate required numeric field
                if billed_amount <= 0:
                    print(f"⚠️ Row {row_idx}: Invalid billed amount for {customer_number}. Skipping.")
                    skipped_count += 1
                    continue

                # --- Room/Property Mapping ---
                # Try to find the room/property for this customer
                # We'll use a lookup approach - first try gas_metrics table
                cur.execute("""
                    SELECT room_id FROM rental_core.gas_metrics 
                    WHERE customer_number = %s
                """, (customer_number,))
                metric_row = cur.fetchone()
                
                room_id = metric_row['room_id'] if metric_row else None
                
                if not room_id:
                    # Try to find by customer_name matching in tenants
                    cur.execute("""
                        SELECT t.room_id, t.tenant_name 
                        FROM rental_core.tenants t
                        WHERE t.tenant_name ILIKE %s
                    """, (f"%{customer_name}%",))
                    tenant_row = cur.fetchone()
                    if tenant_row:
                        room_id = tenant_row['room_id']
                        print(f"  🔍 Matched via tenant name: {tenant_row['tenant_name']}")
                
                # If still no room_id, we still import the bill but without room mapping
                # This allows bills to be tracked even for unassigned connections
                
                # --- Upsert Gas Metric Connection ---
                try:
                    if room_id:
                        cur.execute("""
                            INSERT INTO rental_core.gas_metrics (room_id, customer_number)
                            VALUES (%s, %s)
                            ON CONFLICT (customer_number) DO UPDATE SET room_id = EXCLUDED.room_id;
                        """, (room_id, customer_number))
                    else:
                        # Still upsert the metric without room_id for tracking
                        cur.execute("""
                            INSERT INTO rental_core.gas_metrics (customer_number)
                            VALUES (%s)
                            ON CONFLICT (customer_number) DO NOTHING;
                        """, (customer_number,))
                except Exception as metric_err:
                    print(f"❌ Row {row_idx}: Failed mapping metric row: {metric_err}")
                    conn.rollback()
                    error_count += 1
                    continue

                # --- Determine execution_month from period_to (consumption period) ---
                # Gas bill scraped in September (period 04/06 to 03/09) is billed for August
                # So period_to 2026-09-03 => execution_month 2026-08 (previous month)
                exec_month = execution_month
                if row.get("period_to"):
                    try:
                        period_to_date = datetime.strptime(row.get("period_to", ""), "%Y-%m-%d")
                        # Map to previous month (postpaid: Sep bill is for Aug consumption)
                        prev_month = (period_to_date.replace(day=1) - timedelta(days=1))
                        exec_month = prev_month.strftime("%Y-%m")
                    except ValueError:
                        pass
                
                # --- Check for duplicate (bimonthly billing prevention) ---
                # Check if bill already exists for this customer and period
                # Use enhanced columns if they exist, else fallback to basic customer_number+execution_month
                period_from_parsed = parse_date(row.get("period_from", ""))
                period_to_parsed = parse_date(row.get("period_to", ""))
                
                duplicate_check = False
                # Try enhanced duplicate check only if columns exist
                try:
                    if row.get("latest_invoice_no"):
                        cur.execute("""
                            SELECT id FROM rental_core.gas_bill_master 
                            WHERE customer_number = %s AND invoice_no = %s
                        """, (customer_number, row.get("latest_invoice_no", "")))
                        if cur.fetchone():
                            duplicate_check = True
                            print(f"  ⏭️  Skipping duplicate invoice {row.get('latest_invoice_no')} for {customer_number}")
                            skipped_count += 1
                            continue
                except Exception:
                    pass  # enhanced column not exists, fallback to basic
                    conn.rollback()
                
                if not duplicate_check and period_from_parsed and period_to_parsed:
                    try:
                        cur.execute("""
                            SELECT id FROM rental_core.gas_bill_master 
                            WHERE customer_number = %s AND period_from = %s AND period_to = %s
                        """, (customer_number, period_from_parsed, period_to_parsed))
                        if cur.fetchone():
                            duplicate_check = True
                            print(f"  ⏭️  Skipping duplicate period for {customer_number}")
                            skipped_count += 1
                            continue
                    except Exception:
                        conn.rollback()
                        pass

                # Fallback basic duplicate check (always works)
                if not duplicate_check:
                    cur.execute("""
                        SELECT id FROM rental_core.gas_bill_master 
                        WHERE customer_number = %s AND execution_month = %s
                    """, (customer_number, exec_month))
                    if cur.fetchone():
                        # For basic schema, consider same execution_month as duplicate
                        # But allow update (upsert) rather than skip, so don't skip here
                        pass

                # --- Determine bill type ---
                bill_type = "NORMAL"
                if row.get("previous_paid_date"):
                    pay_date = parse_date(row.get("previous_paid_date", ""))
                    if pay_date:
                        now = datetime.now()
                        start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                        if start_of_this_month.month > 1:
                            start_of_last_month = start_of_this_month.replace(month=start_of_this_month.month - 1)
                        else:
                            start_of_last_month = start_of_this_month.replace(year=start_of_this_month.year - 1, month=12)
                        # Compare as date (pay_date is date, start_of_last_month is datetime)
                        try:
                            pay_date_only = pay_date if isinstance(pay_date, datetime) else datetime.combine(pay_date, datetime.min.time()) if hasattr(pay_date, 'year') and not hasattr(pay_date, 'hour') else pay_date
                            # Ensure both are date for comparison
                            if isinstance(pay_date, datetime):
                                pay_cmp = pay_date.date()
                            else:
                                pay_cmp = pay_date
                            last_month_date = start_of_last_month.date()
                            if pay_cmp < last_month_date:
                                bill_type = "NOTICE"
                        except Exception:
                            pass

                # --- Upsert the Bill Record with all enhanced fields (fallback to basic if enhanced columns missing) ---
                try:
                    # Try enhanced schema first
                    cur.execute("""
                        INSERT INTO rental_core.gas_bill_master 
                            (customer_number, bp_number, customer_name, invoice_no, amount, 
                             partner_name, bill_type, billing_period, period_from, period_to,
                             invoice_due_date, previous_paid_date, previous_paid_amount,
                             last_pay_date, execution_month, source_file, is_bimonthly, created_at)
                        VALUES 
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (customer_number, period_from, period_to) 
                        WHERE period_from IS NOT NULL AND period_to IS NOT NULL
                        DO UPDATE SET 
                            amount = EXCLUDED.amount,
                            bill_type = EXCLUDED.bill_type,
                            invoice_no = EXCLUDED.invoice_no,
                            billing_period = EXCLUDED.billing_period,
                            period_to = EXCLUDED.period_to,
                            invoice_due_date = EXCLUDED.invoice_due_date,
                            previous_paid_date = EXCLUDED.previous_paid_date,
                            previous_paid_amount = EXCLUDED.previous_paid_amount,
                            last_pay_date = EXCLUDED.last_pay_date,
                            bp_number = EXCLUDED.bp_number,
                            customer_name = EXCLUDED.customer_name,
                            source_file = EXCLUDED.source_file,
                            is_bimonthly = EXCLUDED.is_bimonthly,
                            updated_at = NOW();
                    """, (
                        customer_number,
                        bp_number if bp_number else None,
                        customer_name if customer_name else None,
                        row.get("latest_invoice_no", "") if row.get("latest_invoice_no") else None,
                        parse_float(row.get("billed_amount", 0)),
                        "GUJARAT_GAS",
                        bill_type,
                        row.get("billing_period", "") if row.get("billing_period") else None,
                        parse_date(row.get("period_from", "")),
                        parse_date(row.get("period_to", "")),
                        parse_date(row.get("invoice_due_date", "")),
                        parse_date(row.get("previous_paid_date", "")),
                        parse_float(row.get("previous_paid_amount", 0)),
                        parse_date(row.get("previous_paid_date", "")),  # last_pay_date from previous_paid_date
                        exec_month,
                        "gujarat_gas_report.csv",
                        True,  # is_bimonthly
                    ))
                    print(f"✅ Gas Bill Synchronized: {customer_name} ({customer_number}) -> ₹{billed_amount:.2f} [{bill_type}]")
                    success_count += 1
                except Exception as bill_err:
                    # Fallback to basic schema (customer_number, amount, execution_month)
                    if "does not exist" in str(bill_err) or "invoice_no" in str(bill_err) or "period_from" in str(bill_err):
                        try:
                            conn.rollback()
                            cur.execute("SAVEPOINT sp_basic")
                            cur.execute("""
                                INSERT INTO rental_core.gas_bill_master (customer_number, amount, partner_name, bill_type, last_pay_date, execution_month)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (customer_number, execution_month) DO UPDATE SET
                                    amount = EXCLUDED.amount,
                                    bill_type = EXCLUDED.bill_type,
                                    last_pay_date = EXCLUDED.last_pay_date
                            """, (customer_number, parse_float(row.get("billed_amount", 0)), "GUJARAT_GAS", bill_type, parse_date(row.get("previous_paid_date", "")), exec_month))
                            cur.execute("RELEASE SAVEPOINT sp_basic")
                            print(f"✅ Gas Bill Synchronized (basic): {customer_name} ({customer_number}) -> ₹{billed_amount:.2f} [{bill_type}]")
                            success_count += 1
                        except Exception as e2:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_basic")
                            print(f"❌ Row {row_idx}: Database entry synchronization failure for {customer_number}: {e2} (fallback also failed: {bill_err})")
                            conn.rollback()
                            error_count += 1
                            continue
                    else:
                        print(f"❌ Row {row_idx}: Database entry synchronization failure for {customer_number}: {bill_err}")
                        conn.rollback()
                        error_count += 1
                        continue
                    
            except Exception as row_err:
                print(f"❌ Row {row_idx}: Unexpected error: {row_err}")
                conn.rollback()
                error_count += 1
                continue

    conn.commit()
    cur.close()
    conn.close()
    _log(f"\n🎉 Pipeline run completed.")
    _log(f"   ✅ Successful: {success_count}")
    _log(f"   ⏭️  Skipped (duplicates/no-data): {skipped_count}")
    _log(f"   ❌ Errors: {error_count}")
    return {"success": success_count, "skipped": skipped_count, "errors": error_count, "execution_month": execution_month}

if __name__ == "__main__":
    import fcntl
    lock_file = None
    try:
        lock_file = open(str(LOCK_PATH), "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Another gas import is already running. Try again later.", file=sys.stderr)
            sys.exit(0)
        st = _state_init()
        tee = StateTee(sys.stdout, st["run_id"])
        orig_stdout = sys.stdout
        sys.stdout = tee
        import builtins
        raw_print = builtins.print
        def print(*a, **k):  # noqa: A001
            raw_print(*a, **k)
            try:
                tee.log_line(" ".join(str(x) for x in a))
            except:
                pass
        try:
            result = process_gas_pipeline(_tee=tee)
            summary = result if isinstance(result, dict) else {"success": 0}
            _state_finish("finished", summary=summary, run_id=st["run_id"])
            print(f"GAS_IMPORT_REPORT {json.dumps(summary)}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ Import failed: {e}")
            _state_finish("failed", summary={"error": str(e)}, run_id=st["run_id"])
            sys.exit(1)
        finally:
            sys.stdout = orig_stdout
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except:
                pass
            try:
                lock_file.close()
            except:
                pass
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        try:
            if lock_file:
                lock_file.close()
        except:
            pass