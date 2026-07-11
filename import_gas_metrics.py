import os
import csv
import psycopg2
from datetime import datetime

# --- CONFIGURATION ---
DB_PARAMS = {
    "dbname": "tenant_management_db",
    "user": "platform_admin",
    "password": "tenantDB@2026",
    "host": "localhost"
}

CSV_FILENAME = "gujarat_gas_data.csv"

def determine_gas_bill_type(pay_date):
    """Derives notice flags from target date boundaries."""
    if not pay_date:
        return "NORMAL"
        
    now = datetime.now()
    start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    if start_of_this_month.month > 1:
        start_of_last_month = start_of_this_month.replace(month=start_of_this_month.month - 1)
    else:
        start_of_last_month = start_of_this_month.replace(year=start_of_this_month.year - 1, month=12)
        
    if pay_date < start_of_last_month:
        return "NOTICE"
    return "NORMAL"

def clean_field(val):
    """Removes outer double quotes, single quotes, and surrounding whitespace."""
    if not val:
        return ""
    s = val.strip()
    # Strip common enclosing quote variations safely
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s

def process_gas_pipeline():
    if not os.path.exists(CSV_FILENAME):
        print(f"❌ Aborted: Target source file '{CSV_FILENAME}' not found.")
        return

    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    
    execution_month = datetime.now().strftime('%Y-%m')
    print(f"🔄 Processing CSV gas metrics import grid for cycle: {execution_month}...")

    success_count = 0

    with open(CSV_FILENAME, mode='r', encoding='utf-8') as f:
        # csv.reader automatically handles double quotes natively via doublequote=True
        reader = csv.reader(f, delimiter=',', quotechar='"', skipinitialspace=True)
        
        for row_idx, row in enumerate(reader, start=1):
            if not row or len(row) < 4:
                continue
            
            # Run fields through the cleanup layer to catch single quotes or raw text shifts
            plot = clean_field(row[0])
            room = clean_field(row[1])
            customer_number = clean_field(row[2])
            amount_str = clean_field(row[3])
            
            try:
                amount = float(amount_str)
            except ValueError:
                print(f"⚠️ Row {row_idx}: Invalid amount value '{amount_str}'. Skipping.")
                continue

            # Parse optional last payment date column
            pay_date = None
            if len(row) >= 5:
                date_str = clean_field(row[4])
                if date_str:
                    try:
                        pay_date = datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        print(f"⚠️ Row {row_idx}: Invalid date format '{date_str}' (Use YYYY-MM-DD).")

            bill_type = determine_gas_bill_type(pay_date)

            plot_cleaned = plot.replace('-', '').strip() if plot.endswith('-') else plot.strip()
            display_address = f"{plot_cleaned} / {room}"

            # Step A: Room Profile Verification/Instantiation
            cur.execute("SELECT id FROM rental_core.rooms WHERE display_address = %s", (display_address,))
            room_row = cur.fetchone()
            
            if room_row:
                room_id = room_row[0]
            else:
                cur.execute("SELECT id FROM rental_core.property_masters WHERE plot_number = %s", (plot_cleaned,))
                master_row = cur.fetchone()
                
                if not master_row:
                    print(f"❌ Row {row_idx}: Plot Context '{plot_cleaned}' missing in property_masters.")
                    continue
                
                master_id = master_row[0]
                
                try:
                    cur.execute("""
                        INSERT INTO rental_core.rooms (display_address, master_id, flat_number)
                        VALUES (%s, %s, %s) RETURNING id;
                    """, (display_address, master_id, room))
                    room_id = cur.fetchone()[0]
                    print(f"🔨 Instantiated missing room mapping: '{display_address}'")
                except Exception as room_err:
                    print(f"❌ Row {row_idx}: Failed to auto-generate room mapping: {room_err}")
                    continue

            # Step B: Upsert the Gas Metric Connection
            try:
                cur.execute("""
                    INSERT INTO rental_core.gas_metrics (room_id, customer_number)
                    VALUES (%s, %s)
                    ON CONFLICT (customer_number) DO UPDATE SET room_id = EXCLUDED.room_id;
                """, (room_id, customer_number))
            except Exception as metric_err:
                print(f"❌ Row {row_idx}: Failed mapping metric row: {metric_err}")
                conn.rollback()
                continue

            # Step C: Upsert the Bill Record
            try:
                cur.execute("""
                    INSERT INTO rental_core.gas_bill_master 
                        (customer_number, amount, partner_name, bill_type, last_pay_date, execution_month, created_at)
                    VALUES 
                        (%s, %s, 'GUJARAT_GAS', %s, %s, %s, NOW())
                    ON CONFLICT (customer_number, execution_month) 
                    DO UPDATE SET 
                        amount = EXCLUDED.amount,
                        bill_type = EXCLUDED.bill_type,
                        last_pay_date = EXCLUDED.last_pay_date,
                        created_at = NOW();
                """, (customer_number, amount, bill_type, pay_date.date() if pay_date else None, execution_month))
                
                print(f"✅ Gas Bill Synchronized: {display_address} -> ₹{amount} [{bill_type}]")
                success_count += 1
            except Exception as bill_err:
                print(f"❌ Row {row_idx}: Database entry synchronization failure: {bill_err}")
                conn.rollback()
                continue

    conn.commit()
    cur.close()
    conn.close()
    print(f"\n🎉 Pipeline run completed. Successfully updated {success_count} gas statements.")

if __name__ == "__main__":
    process_gas_pipeline()