import pandas as pd
import logging
from db_config import get_db_connection

# --- CONFIGURATION ---

# Setup Logging
logging.basicConfig(filename='debug_import.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def perform_mapping():
    # 1. Load CSVs with single-quote quotechar handling
    # Assumes files are comma separated and fields are wrapped in single quotes
    tenant_df = pd.read_csv("tenant_service.csv", quotechar="'")
    enc1 = pd.read_csv("service_enc_1.csv", names=['service', 'enc'], header=None, quotechar="'")
    enc2 = pd.read_csv("service_enc_2.csv", names=['service', 'enc'], header=None, quotechar="'")
    
    # Add account_id columns
    enc1['account_id'] = 1
    enc2['account_id'] = 2
    full_enc_df = pd.concat([enc1, enc2])

    conn = get_db_connection()
    cur = conn.cursor()

    # 2. Process rows
    for _, row in tenant_df.iterrows():
        try:
            meter = str(row['METER NO']).strip()
            plot = str(row['PLOT']).strip()
            flat = str(row['ROOM/FLAT']).strip()
            display_addr = f"{plot} / {flat}"

            # Find matching encrypted value
            # Ensure types match (convert both to string for reliable comparison)
            match = full_enc_df[full_enc_df['service'].astype(str) == meter]
            
            if match.empty:
                raise ValueError(f"No encrypted value found for meter {meter}")
            
            enc_val = match.iloc[0]['enc']
            acc_id = int(match.iloc[0]['account_id'])

            # 3. Insert into utility_metrics
            cur.execute("""
                INSERT INTO rental_core.utility_metrics (room_id, service_number, account_id, encrypted_value)
                VALUES (
                    (SELECT id FROM rental_core.rooms WHERE display_address = %s),
                    %s, %s, %s
                )
            """, (display_addr, meter, acc_id, enc_val))
            
            logging.info(f"SUCCESS: Inserted meter {meter} for {display_addr}")

        except Exception as e:
            logging.error(f"FAILED: Meter {meter} ({display_addr}) - Error: {e}")
            conn.rollback() 

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Mapping finished. Check debug_import.log for details.")

if __name__ == "__main__":
    perform_mapping()