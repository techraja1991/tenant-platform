import os
import json
import base64
import psycopg2
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- CONFIGURATION ---
DB_PARAMS = {"dbname": "tenant_management_db", "user": "platform_admin", "password": "tenantDB@2026", "host": "localhost"}
AES_KEY = b"00000000000000000MAKV2SPBNI99212"

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

class EncryptionHelper:
    def encrypt(self, plaintext_str):
        aesgcm = AESGCM(AES_KEY)
        iv = os.urandom(12)
        encrypted_bytes = iv + aesgcm.encrypt(iv, plaintext_str.encode('utf-8'), None)
        return base64.b64encode(encrypted_bytes).decode('utf-8')

def insert_and_encrypt_utility_data():
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cipher = EncryptionHelper()

    print(f"Starting processing for {len(data)} utility metrics...")

    for plot, room, meter_no in data:
        plot_cleaned = plot.replace('-', '').strip() if plot.endswith('-') else plot.strip()
        # 1. Route account_id based on your logic
        account_id = 2 if plot in ['56', '57', '56-57'] else 1
        
        # 2. Build the exact display address structure
        display_address = f"{plot} / {room}"
        room_id = None
        # 1. Route Special Utility Rules
        if room == 'WATER':
            room_id = 0
            print(f"💧 Mapped common utility meter {meter_no} to system room ID 0")
        else:
            # 2. Look up or dynamically generate physical rooms
            # 3. Match against the rooms master table to find the room ID
            cur.execute("SELECT id FROM rental_core.rooms WHERE display_address = %s", (display_address,))
            room_row = cur.fetchone()
            
            if room_row:
                room_id = room_row[0]
            else:
                # 3. Dynamic Room Creation Layer: Fetch master_id from property_masters
                cur.execute(
                    "SELECT id FROM rental_core.property_masters WHERE plot_number = %s", 
                    (plot_cleaned,)
                )
                master_row = cur.fetchone()
                
                if not master_row:
                    print(f"❌ Aborted room creation: Plot '{plot_cleaned}' not found in rental_core.property_masters.")
                    continue
                
                master_id = master_row[0]
                
                try:
                    # Insert room tracking master_id and flat_number
                    cur.execute("""
                        INSERT INTO rental_core.rooms (display_address, master_id, flat_number) 
                        VALUES (%s, %s, %s) 
                        RETURNING id;
                    """, (display_address, master_id, room))
                    room_id = cur.fetchone()[0]
                    print(f"🔨 Created missing room record: '{display_address}' (Master ID: {master_id}, Room: {room})")
                except Exception as room_err:
                    print(f"❌ Failed to create room row for '{display_address}': {room_err}")
                    continue
            # 3. Handle data metrics insertion/updates
        if room_id is not None:
            try:
                # 4. Generate the payload matching the Torrent Power API format
                payload_json = json.dumps({"serviceNo": str(meter_no), "city": "SRT"})
                encrypted_value = cipher.encrypt(payload_json)
                
                # 5. Insert or update the records securely
                cur.execute("""
                    INSERT INTO rental_core.utility_metrics (account_id, room_id, service_number, encrypted_value)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (service_number) DO UPDATE 
                    SET encrypted_value = EXCLUDED.encrypted_value,
                        account_id = EXCLUDED.account_id,
                        room_id = EXCLUDED.room_id;
                """, (account_id, room_id, meter_no, encrypted_value))
                
                print(f"✅ Processed: {display_address} -> Meter: {meter_no}")
            except Exception as e:
                print(f"❌ Database execution failed for meter {meter_no}: {e}")
        else:
            print(f"⚠️ Warning: No match found in rental_core.rooms for display_address: '{display_address}'")

    conn.commit()
    cur.close()
    conn.close()
    print("🎉 Data population complete.")

if __name__ == "__main__":
    insert_and_encrypt_utility_data()