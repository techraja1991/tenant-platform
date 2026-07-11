import psycopg2
import pandas as pd
import math

DB_PARAMS = {
    "dbname": "tenant_management_db",
    "user": "platform_admin",
    "password": "tenantDB@2026",  # <-- Put your updated password here
    "host": "localhost",
    "port": "5432",
    "options": "-c search_path=rental_core"
}

def clean_currency(value):
    """Removes commas and handles empty/NaN values from the spreadsheet"""
    if pd.isna(value) or value == '':
        return 0.00
    if isinstance(value, str):
        value = value.replace(',', '').strip()
    return float(value)

def run_import():
    conn = None
    try:
        print("📂 Loading tenant_data.csv...")
        df = pd.read_csv("tenant_data.csv", skiprows=1)
        df.columns = df.columns.str.strip()
        df = df.dropna(subset=['Plot', 'Flat', 'Tenant Name'])

        conn = psycopg2.connect(**DB_PARAMS)
        cursor = conn.cursor()
        print("🔗 Connected to database. Beginning mass ingestion...")

        master_cache = {}

        for index, row in df.iterrows():
            # Treat Plot and Flat as text to safely handle '56-57' or alphanumeric flat numbers
            plot_num = str(row['Plot']).replace('.0', '').strip()
            flat_num = str(row['Flat']).replace('.0', '').strip()
            name = str(row['Tenant Name']).strip()
            
            phone = str(row['phone']).replace('.0', '').strip()
            
            rent = clean_currency(row['Bhadu'])
            maintenance = clean_currency(row['Maintenance'])
            prev_baki = clean_currency(row['Prev Baki'])
            light_bill = clean_currency(row['Light Bill'])
            
            # 1. Ensure the Property Master exists for this Plot
            if plot_num not in master_cache:
                cursor.execute("""
                    INSERT INTO rental_core.property_masters (apartment_name, plot_number, default_base_rent, default_maintenance_fee, default_security_deposit)
                    VALUES (%s, %s, %s, 0.00, 5000.00)
                    ON CONFLICT (plot_number) DO UPDATE SET default_base_rent = EXCLUDED.default_base_rent
                    RETURNING id;
                """, (f"Plot {plot_num} Apartment", plot_num, rent))
                master_cache[plot_num] = cursor.fetchone()[0]
            
            master_id = master_cache[plot_num]
            display_addr = f"{plot_num} / {flat_num}"

            # 2. Insert the physical room profile
            cursor.execute("""
                INSERT INTO rental_core.rooms (master_id, flat_number, display_address)
                VALUES (%s, %s, %s)
                ON CONFLICT (display_address) DO UPDATE SET flat_number = EXCLUDED.flat_number
                RETURNING id;
            """, (master_id, flat_num, display_addr))
            room_id = cursor.fetchone()[0]

            # 3. Create the active Tenant profile
            cursor.execute("""
                INSERT INTO rental_core.tenants (room_id, tenant_name, phone_number, is_active, base_rent_applied, maintenance_applied, security_deposit_applied)
                VALUES (%s, %s, %s, 'Y', %s, %s, 5000.00)
                RETURNING id;
            """, (room_id, name, phone, rent, maintenance))
            tenant_id = cursor.fetchone()[0]

            # 4. Generate the current ledger row (Jun-26)
            cursor.execute("""
                INSERT INTO rental_core.monthly_ledgers (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied, light_bill, gas_bill, maintenance_applied)
                VALUES (%s, %s, 'Jun-26', %s, 0.00, %s, %s, 0.00, %s);
            """, (room_id, tenant_id, prev_baki, rent, light_bill, maintenance))

        conn.commit()
        print(f"🎉 Mass Ingestion complete! Successfully loaded {len(df)} tenant records into the matrix.")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ Ingestion failed: {e}")
    finally:
        if conn:
            cursor.close()
            conn.close()         
if __name__ == "__main__":
    run_import()