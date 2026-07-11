import os
import csv
import psycopg2

# --- DATABASE CONFIGURATION ---
DB_PARAMS = {
    "dbname": "tenant_management_db",
    "user": "platform_admin",
    "password": "tenantDB@2026",
    "host": "localhost"
}

# Raw structure data feed (Plot, Room/Flat, Meter Number)
data = [
    ('56','1','500542469'), ('56','2','500542470'), ('56','3','500542471'), ('56','4','500542472'),
    ('56','5','500542473'), ('56','6','500543498'), ('56','7','500542468'), ('56','8','500542474'),
    ('56','9','500547233'), ('56','10','500547241'), ('56','11','500547244'), ('56','12','500547247'),
    ('56','13','500547235'), ('56','14','500547236'), ('56','15','500547238'), ('56','16','500547240'),
    ('57','1','500542467'), ('57','2','500542466'), ('57','3','500542475'), ('57','4','500543499'),
    ('57','5','500542476'), ('57','6','500543497'), ('57','7','500547232'), ('57','8','500547243'),
    ('57','9','500547242'), ('57','10','500547245'), ('57','11','500547234'), ('57','12','500547239'),
    ('57','13','500547237'), ('57','14','500547246'), ('56-57','SHOP 1','500542465'), ('53','G1','500222392'),
    ('53','101','500206734'), ('53','201','500193678'), ('53','301','501070083'), ('53','401','501070082'),
    ('53','WATER','500292587'), ('54','G1','500484531'), ('54','G2','500484532'), ('54','G3','500484533'),
    ('54','101-RAJA','500444302'), ('54','203','500444304'), ('54','204','500487698'), ('54','301','500444306'),
    ('54','302','500487699'), ('54','303','500444305'), ('54','304','500484535'), ('VAIBHAV','SHOP 1','501071080'),
    ('VAIBHAV','SHOP 2','501071081'), ('VAIBHAV','SHOP 3','501071082'), ('SURYAKIRAN','SHOP 2','500240215'),
    ('SURYAKIRAN','108','500240228'), ('SURYAKIRAN','303','500240239'), ('LAKSHMI CHAMBERS','101','500215823'),
    ('HAREKRUSHNA','G1-OFFICE-JALARAM VISION','500372179'), ('35','G1','500425295'), ('35','101','500193398'),
    ('35','201','500446991'), ('35','301','500450396'), ('35','WATER','500446990')
]

def load_encrypted_values():
    """Loads pre-calculated encrypted tokens from headerless CSV files into a fast lookup dict."""
    encrypted_lookup = {}
    
    for filename in ['account_1.csv', 'account_2.csv']:
        if not os.path.exists(filename):
            print(f"⚠️ Warning: Reference file '{filename}' missing. Skipping lookup load.")
            continue
            
        with open(filename, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                # Skip blank lines safely
                if not row or len(row) < 2:
                    continue
                
                # Directly map columns by index array positions: 0 = meter, 1 = encrypted value
                service_no = row[0].strip()
                enc_val = row[1].strip()
                encrypted_lookup[service_no] = enc_val
                
    print(f"📂 Loaded {len(encrypted_lookup)} pre-calculated metric tokens from headerless CSV sources.")
    return encrypted_lookup

def process_pipeline():
    # 1. Initialize data lookups
    csv_tokens = load_encrypted_values()
    
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()

    print(f"🔄 Processing processing grid ({len(data)} items)...")

    for plot, room, meter_no in data:
        # Clean input variations (e.g. trailing hyphens)
        plot_cleaned = plot.replace('-', '').strip() if plot.endswith('-') else plot.strip()
        meter_no = meter_no.strip()
        
        account_id = 2 if plot_cleaned in ['56', '57', '56-57'] else 1
        display_address = f"{plot_cleaned} / {room}"
        room_id = None

        # Step A: Check for pre-calculated token asset
        if meter_no not in csv_tokens:
            print(f"❌ Skipped: No pre-encrypted token found in CSVs for Meter '{meter_no}' ({display_address})")
            continue
        encrypted_value = csv_tokens[meter_no]

        # Step B: Route Special Non-Rentable Utility Meters
        if room == 'WATER':
            room_id = 0
            print(f"💧 Mapped common utility meter {meter_no} to System Room ID 0")
        else:
            # Step C: Check if room profile exists
            cur.execute("SELECT id FROM rental_core.rooms WHERE display_address = %s", (display_address,))
            room_row = cur.fetchone()
            
            if room_row:
                room_id = room_row[0]
            else:
                # Step D: Pull associated plot context from master index
                cur.execute(
                    "SELECT id FROM rental_core.property_masters WHERE plot_number = %s", 
                    (plot_cleaned,)
                )
                master_row = cur.fetchone()
                
                if not master_row:
                    print(f"❌ Aborted Room Creation: Plot '{plot_cleaned}' not found in property_masters.")
                    continue
                
                master_id = master_row[0]
                
                try:
                    # Dynamically instantiate structural room profiles safely
                    cur.execute("""
                        INSERT INTO rental_core.rooms (display_address, master_id, flat_number) 
                        VALUES (%s, %s, %s) 
                        RETURNING id;
                    """, (display_address, master_id, room))
                    room_id = cur.fetchone()[0]
                    print(f"🔨 Created missing room record: '{display_address}' (Master ID: {master_id}, Flat: {room})")
                except Exception as room_err:
                    print(f"❌ Failed to instantiate room row for '{display_address}': {room_err}")
                    continue

        # Step E: Synchronize metrics metrics via database upsert pattern
        if room_id is not None:
            try:
                cur.execute("""
                    INSERT INTO rental_core.utility_metrics (account_id, room_id, service_number, encrypted_value)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (service_number) DO UPDATE 
                    SET encrypted_value = EXCLUDED.encrypted_value,
                        account_id = EXCLUDED.account_id,
                        room_id = EXCLUDED.room_id;
                """, (account_id, room_id, meter_no, encrypted_value))
                
                print(f"✅ Metric Synchronized: {display_address} [Room ID: {room_id}]")
            except Exception as metric_err:
                print(f"❌ Database write failed for service number {meter_no}: {metric_err}")

    conn.commit()
    cur.close()
    conn.close()
    print("🎉 Pipeline executed cleanly. Database values synchronized directly using CSV tokens.")

if __name__ == "__main__":
    process_pipeline()