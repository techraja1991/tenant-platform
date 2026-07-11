import os
import base64
import requests
import psycopg2
import logging
import csv
import json
import re
import time
import random
from datetime import datetime

# --- CONFIGURATION ---
DB_PARAMS = {"dbname": "tenant_management_db", "user": "platform_admin", "password": "tenantDB@2026", "host": "localhost"}

logging.basicConfig(filename='automation_engine.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

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

def run_multi_account_pipeline():
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    execution_month = datetime.now().strftime('%Y-%m')
    
    with open('monthly_results.csv', 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Service', 'Partner', 'Amount', 'Type'])

        cur.execute("SELECT id, access_token FROM rental_core.torrent_accounts")
        accounts = cur.fetchall()
        print(f"🔗 Found {len(accounts)} accounts to process for month {execution_month}.")
        
        for acc_id, access_token in accounts:
            engine = TorrentEngine(access_token)
            print(f"🚀 Processing account {acc_id} with access token {access_token[:10]}...")
            
            # Select pre-calculated text tokens from utility_metrics
            cur.execute("SELECT service_number, encrypted_value FROM rental_core.utility_metrics WHERE account_id = %s", (acc_id,))
            services = cur.fetchall()
            print(f"📡 Processing {len(services)} services for account {acc_id}.")
            
            for (service, encrypted_value) in services:
                if not encrypted_value:
                    print(f"⚠️ Skipped: No pre-encrypted token value found in DB for Service {service}")
                    continue
                    
                try:
                    # --- ANTI-BOT DYNAMIC THROTTLING DELAY ---
                    sleep_duration = random.uniform(2.0, 4.0)
                    print(f"⏳ Pausing for {sleep_duration:.2f} seconds to protect API rate limits...")
                    time.sleep(sleep_duration)

                    print(f"🔒 Utilizing persistent DB token for service {service}: {encrypted_value[:20]}...")
                    
                    # 1. Fetch live metrics directly using pre-saved token assets
                    resp = engine.get_amount(encrypted_value)
                    data = resp.get("d", {})
                    
                    amt = data.get("OsAmt")
                    partner = data.get("Partner")
                    last_pay = data.get("LastPay")
                    bill_type = determine_bill_type(last_pay)
                    
                    pay_date = None
                    if last_pay:
                        match = re.search(r'/Date\((\d+)\)/', last_pay)
                        if match:
                            pay_date = datetime.fromtimestamp(int(match.group(1)) / 1000.0)

                    print(f"Service: {service}, Amount: {amt}, Partner: {partner}, Type: {bill_type}, Last Payment: {pay_date}")
                    
                    # 2. Database Upsert Engine (Updates if service_number + execution_month combination already exists)
                    cur.execute("""
                        INSERT INTO rental_core.torrent_bill_master 
                            (service_number, amount, partner_name, bill_type, last_pay_date, execution_month, created_at)
                        VALUES 
                            (%s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (service_number, execution_month) 
                        DO UPDATE SET 
                            amount = EXCLUDED.amount,
                            partner_name = EXCLUDED.partner_name,
                            bill_type = EXCLUDED.bill_type,
                            last_pay_date = EXCLUDED.last_pay_date,
                            created_at = NOW()
                    """, (service, amt, partner, bill_type, pay_date, execution_month))
                    
                    writer.writerow([service, partner, amt, bill_type])
                    
                except Exception as e:
                    logging.error(f"Pipeline failed for service {service}: {e}")
                    print(f"❌ Error for service {service}: {e}")
                    # Ensure database remains transactional if error loops inside iterations
                    conn.rollback()

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Master bills for {execution_month} populated and upserted successfully.")

if __name__ == "__main__":
     run_multi_account_pipeline()