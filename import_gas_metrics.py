import os
import csv
from datetime import datetime
from db_config import get_db_connection

# --- CONFIGURATION ---
CSV_FILENAME = "gujarat_gas_report.csv"

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

def process_gas_pipeline():
    if not os.path.exists(CSV_FILENAME):
        print(f"❌ Aborted: Target source file '{CSV_FILENAME}' not found.")
        return

    conn = get_db_connection()
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
                
                # Extract customer_number from bp_number or account_number
                customer_number = bp_number if bp_number else account_number
                
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
                # For bimonthly bills, use period_to as the execution month
                exec_month = execution_month
                if row.get("period_to"):
                    try:
                        period_to_date = datetime.strptime(row.get("period_to", ""), "%Y-%m-%d")
                        exec_month = period_to_date.strftime("%Y-%m")
                    except ValueError:
                        pass
                
                # --- Check for duplicate (bimonthly billing prevention) ---
                # Check if bill already exists for this customer and period
                period_from_parsed = parse_date(row.get("period_from", ""))
                period_to_parsed = parse_date(row.get("period_to", ""))
                
                duplicate_check = False
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
                
                if not duplicate_check and period_from_parsed and period_to_parsed:
                    cur.execute("""
                        SELECT id FROM rental_core.gas_bill_master 
                        WHERE customer_number = %s AND period_from = %s AND period_to = %s
                    """, (customer_number, period_from_parsed, period_to_parsed))
                    if cur.fetchone():
                        duplicate_check = True
                        print(f"  ⏭️  Skipping duplicate period for {customer_number}")
                        skipped_count += 1
                        continue

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
                        if pay_date < start_of_last_month:
                            bill_type = "NOTICE"

                # --- Upsert the Bill Record with all enhanced fields ---
                try:
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
    print(f"\n🎉 Pipeline run completed.")
    print(f"   ✅ Successful: {success_count}")
    print(f"   ⏭️  Skipped (duplicates/no-data): {skipped_count}")
    print(f"   ❌ Errors: {error_count}")

if __name__ == "__main__":
    process_gas_pipeline()