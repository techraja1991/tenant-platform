import os
import base64
import requests
import psycopg2
import logging
import csv
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from datetime import datetime
import re


# --- CONFIGURATION ---
DB_PARAMS = {"dbname": "tenant_management_db", "user": "platform_admin", "password": "tenantDB@2026", "host": "localhost"}
AES_KEY = b"00000000000000000MAKV2SPBNI99212"
print(f"DEBUG: AES_KEY length is {len(AES_KEY)} bytes") # This should print 32
if len(AES_KEY) != 32:
    raise ValueError("AES_KEY must be exactly 32 bytes!")
logging.basicConfig(filename='automation_engine.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

class TorrentEngine:
    def __init__(self, login_service_no, password):
        self.session = requests.Session()
        self.base_url = "https://connect.torrentpower.com"
        self.login_service_no = login_service_no
        self.password = password
        self.token = None

    def encrypt(self, plaintext_str):
        try:
            aesgcm = AESGCM(AES_KEY)
            iv = os.urandom(12)
            encrypted_bytes = iv + aesgcm.encrypt(iv, plaintext_str.encode('utf-8'), None)
            return base64.b64encode(encrypted_bytes).decode('utf-8')
        except Exception as e:
            logging.error(f"Encryption failed for input: {plaintext_str}. Error: {e}")
            raise 

    def login(self):
        url = "https://connect.torrentpower.com/authentication/api/v1/user/login"
        
        # Construct the payload using the login_service_no
        payload = {
            "encryptFlag": "1",  # Note the capital F
            "encrypt": self.encrypt(json.dumps({
                "field": self.login_service_no,
                "password": self.password,
                "city": "SRT",
                "logoutFromAllDevice": False,
                "deviceDetails": "portal",
                "ipAddress": "10.2.2",
                "source": "portal"
            }))
        }
        
        # # Encrypt the full JSON payload
        # try:
        #     print(f"🔐 Logging in with service number: {self.login_service_no}")
        #     encrypted_payload = self.encrypt(json.dumps(payload))
        #     print(f"Encrypted payload: {encrypted_payload}")
        #     request_body = {"encryptflag": "1", "encrypt": encrypted_payload}
        # except Exception as e:
        #     logging.error(f"Error occurred while encrypting payload for account {self.login_service_no}: {e}")
        #     raise

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Bearer ojMS+klC+TZjb6i/uLJNpQLN90d+J4ROkVyLwLVKOKUkmAR5YyeeS/mREwcSzprpkV0tF5cyMvi/pgSavt8/",
            "Content-Type": "application/json",
            "Referer": "https://connect.torrentpower.com/tplcp/session/signin",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        }
        print(f"Sending login request to {url} with headers {headers} and payload {payload}")
        resp = self.session.post(url, json=payload, headers=headers)
        print(f"Login response status code: {resp.status_code}, response text: {resp.text}")
        data = resp.json()
        print(f"Login response: {data}")
        
        if "encodeResponseData" not in data:
            raise Exception(f"Login failed: No encodeResponseData in response. {data}")
            
        # You now have the encrypted blob. 
        # Proceed to decryption logic to extract 'accessToken'
        print("✅ Received encrypted response.")

    def get_amount(self, enc_service):
        url = f"{self.base_url}/payment/payment/getamount"
        return self.session.post(url, json={"encryptFlag":"1", "encrypt": enc_service}).json()

def determine_bill_type(last_pay_str):
    if not last_pay_str: return "NORMAL"
    match = re.search(r'/Date\((\d+)\)/', last_pay_str)
    if not match: return "NORMAL"
    pay_date = datetime.fromtimestamp(int(match.group(1)) / 1000.0)
    now = datetime.now()
    start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_of_last_month = (start_of_this_month.replace(month=start_of_this_month.month - 1) 
                           if start_of_this_month.month > 1 else start_of_this_month.replace(year=start_of_this_month.year-1, month=12))
    return "NOTICE" if pay_date < start_of_last_month else "NORMAL"

# ... [Imports and TorrentEngine class remain the same] ...

def run_multi_account_pipeline():
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    execution_month = datetime.now().strftime('%Y-%m')
    
    with open('monthly_results.csv', 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Service', 'Partner', 'Amount', 'Type'])

        cur.execute("SELECT id,account_name,login_service_number, password FROM rental_core.torrent_accounts")
        accounts = cur.fetchall()

        for acc_id, account_name, login_service, password in accounts:
            print (f"Processing account {account_name} ({login_service}) and {password}...")
            try:
                engine = TorrentEngine(login_service, password)
                engine.login()
                
                cur.execute("SELECT service_number FROM rental_core.utility_metrics WHERE account_id = %s", (acc_id,))
                services = cur.fetchall()

                for (service,) in services:
                    enc_service = engine.encrypt(service)
                    resp = engine.get_amount(enc_service)
                    data = resp.get("d", {})
                    
                    amt = data.get("OsAmt")
                    partner = data.get("Partner")
                    last_pay = data.get("LastPay")
                    bill_type = determine_bill_type(last_pay)
                    print (f"Service: {service}, Amount: {amt}, Partner: {partner}, Type: {bill_type}")
                    # INSERT into Bill Master (The staging/master record)
                    cur.execute("""
                        INSERT INTO rental_core.torrent_bill_master 
                        (service_number, amount, partner_name, bill_type, execution_month)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (service, amt, partner, bill_type, execution_month))
                    
                    writer.writerow([service, partner, amt, bill_type])
                    
            except Exception as e:
                logging.error(f"Pipeline failed for account {account_name}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Master bills for {execution_month} populated.")

if __name__ == "__main__":
    run_multi_account_pipeline()