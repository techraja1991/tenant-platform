import csv
from datetime import datetime, timedelta
from db_config import get_db_connection

# Get current billing month in YYYY-MM format
billing_month = datetime.now().strftime('%Y-%m')
billing_month_display = datetime.now().strftime('%b-%y')

# Calculate the previous month string for rolling over debt (prev_baki)
prev_month_dt = datetime.now().replace(day=1) - timedelta(days=1)
prev_billing_month = prev_month_dt.strftime('%Y-%m')

# Output CSV file
output_csv = f"tenant_bills_{billing_month}.csv"

conn = None
try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Fetch all active tenants alongside their property configurations
    cursor.execute(
        """
        SELECT 
            t.id AS tenant_pk, 
            t.tenant_name,
            t.phone_number,
            t.is_active,
            t.base_rent_applied,
            t.maintenance_applied,
            r.id AS room_id,
            r.flat_number,
            r.master_id,
            r.display_address,
            pm.plot_number,
            pm.default_maintenance_fee
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.is_active = 'Y'
        ORDER BY pm.plot_number, r.flat_number
        """
    )
    tenants = cursor.fetchall()

    # Cache for tracking dynamic maintenance distributions per property this month
    property_maintenance_cache = {}

    # CSV output
    csv_rows = []
    sr_no = 0

    for (tenant_pk, tenant_name, phone_number, is_active, base_rent_applied, 
         tenant_maintenance_applied, room_id, flat_number, master_id, 
         display_address, plot_number, default_maintenance_fee) in tenants:
        
        # 2. Fixed maintenance per unit - added every month
        fixed_maintenance = tenant_maintenance_applied if tenant_maintenance_applied > 0 else default_maintenance_fee

        # 2b. Shared misc expenses per property (split among active tenants), OWNER expenses excluded
        if master_id not in property_maintenance_cache:
            # Sum all tenant-recoverable SHARED expenses for this property this month
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0.00) 
                FROM rental_core.property_maintenance_expenses
                WHERE master_id = %s AND execution_month = %s
                  AND COALESCE(charge_type, 'SHARED') = 'SHARED'
                """,
                (master_id, billing_month)
            )
            total_shared_cost = cursor.fetchone()[0]

            # Count active tenants under this specific property framework
            cursor.execute(
                """
                SELECT COUNT(*) FROM rental_core.tenants t
                JOIN rental_core.rooms r ON t.room_id = r.id
                WHERE r.master_id = %s AND t.is_active = 'Y'
                """,
                (master_id,)
            )
            tenant_count_for_prop = cursor.fetchone()[0]

            # Divide evenly among active tenants for this property
            share = total_shared_cost / tenant_count_for_prop if tenant_count_for_prop > 0 else 0.00
            property_maintenance_cache[master_id] = share

        shared_misc = property_maintenance_cache[master_id]

        # 2c. Unit-specific misc expenses for this room (OWNER expenses excluded)
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0.00) 
            FROM rental_core.property_maintenance_expenses
            WHERE room_id = %s AND execution_month = %s AND charge_type = 'UNIT'
            """,
            (room_id, billing_month)
        )
        unit_misc = cursor.fetchone()[0]

        # Total tenant-recoverable misc expenses for this tenant this month
        common_misc_expenses = shared_misc + unit_misc

        # 3. Get previous month's remaining balance (new_baki) to roll over into prev_baki
        cursor.execute(
            """
            SELECT COALESCE(new_baki, 0.00), COALESCE(prev_jama, 0.00) FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
            """,
            (tenant_pk, prev_billing_month)
        )
        row = cursor.fetchone()
        prev_baki = row[0] if row else 0.00
        prev_jama = row[1] if row else 0.00

        # 3b. Get current month's payment (jama) for accurate new_baki on re-runs
        cursor.execute(
            """
            SELECT COALESCE(jama, 0.00) FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
            """,
            (tenant_pk, billing_month)
        )
        jama_row = cursor.fetchone()
        current_jama = jama_row[0] if jama_row else 0.00

        # 4. Get room-specific direct utility bills using metrics joins
        cursor.execute(
            """
            SELECT 
                COALESCE(SUM(CASE 
                    WHEN tbm.bill_type = 'NOTICE' 
                        THEN GREATEST(tbm.amount - COALESCE(prev.amount, 0.00), 0.00)
                    ELSE tbm.amount END), 0.00),
                COALESCE(BOOL_OR(tbm.bill_type = 'NOTICE'), FALSE)
            FROM rental_core.torrent_bill_master tbm
            JOIN rental_core.utility_metrics um ON tbm.service_number = um.service_number
            LEFT JOIN rental_core.torrent_bill_master prev 
                ON prev.service_number = tbm.service_number 
                AND prev.execution_month = %s
            WHERE um.room_id = %s AND tbm.execution_month = %s
            """,
            (prev_billing_month, room_id, billing_month)
        )
        light_bill_row = cursor.fetchone()
        light_bill = light_bill_row[0]
        light_bill_type = "NOTICE" if light_bill_row[1] else "NORMAL"

        cursor.execute(
            """
            SELECT COALESCE(SUM(gbm.amount), 0.00) 
            FROM rental_core.gas_bill_master gbm
            JOIN rental_core.gas_metrics gm ON gbm.customer_number = gm.customer_number
            WHERE gm.room_id = %s AND gbm.execution_month = %s
            """,
            (room_id, billing_month)
        )
        gas_bill = cursor.fetchone()[0]

        # 5. Calculate derived fields
        # Month Total = Bhadu + Light Bill + Gas Bill + Maintenance + Misc Expenses
        month_total = base_rent_applied + light_bill + gas_bill + fixed_maintenance + common_misc_expenses
        
        # Gross Baki = Prev Baki - Prev Jama + Month Total
        gross_baki = prev_baki - prev_jama + month_total
        
        # New Baki = Gross Baki - Jama (current month payment)
        new_baki = gross_baki - current_jama
        
        # For CSV - maintenance value (fixed per-unit monthly maintenance)
        maintenance_for_csv = fixed_maintenance

        # 6. UPSERT into monthly_ledgers
        cursor.execute(
            """
            INSERT INTO rental_core.monthly_ledgers (
                room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied, 
                light_bill, light_bill_type, gas_bill, maintenance_applied, common_misc_expenses
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (tenant_id, billing_month) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                prev_baki = EXCLUDED.prev_baki,
                prev_jama = EXCLUDED.prev_jama,
                bhadu_applied = EXCLUDED.bhadu_applied,
                light_bill = EXCLUDED.light_bill,
                light_bill_type = EXCLUDED.light_bill_type,
                gas_bill = EXCLUDED.gas_bill,
                maintenance_applied = EXCLUDED.maintenance_applied,
                common_misc_expenses = EXCLUDED.common_misc_expenses
            """,
            (
                room_id, tenant_pk, billing_month, prev_baki, prev_jama, base_rent_applied,
                light_bill, light_bill_type, gas_bill, fixed_maintenance, common_misc_expenses
            )
        )

        # 7. Prepare CSV row
        sr_no += 1
        csv_rows.append([
            sr_no,
            plot_number,
            flat_number,
            tenant_name,
            phone_number,
            is_active,
            display_address,
            f"{prev_baki:.2f}",
            f"{prev_jama:.2f}",
            f"{base_rent_applied:.2f}",
            f"{light_bill:.2f}",
            f"{gas_bill:.2f}",
            f"{maintenance_for_csv:.2f}",
            f"{current_jama:.2f}",  # Jama (current month payment)
            "",      # Jama Date
            f"{month_total:.2f}",
            f"{gross_baki:.2f}",
            f"{new_baki:.2f}"
        ])

    # Write CSV file
    with open(output_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            'SrNo', 'Plot', 'Flat', 'Tenant Name', 'phone', 'IS_ACTIVE', 'Flat No',
            'Prev Baki', 'Prev Jama', 'Bhadu', 'Light Bill', 'Gas Bill', 'Maintenance',
            'Jama', 'Jama Date', 'Month Total', 'GROSS BAKI', 'New Baki'
        ])
        writer.writerows(csv_rows)

    conn.commit()
    print(f"🎉 Successfully allocated bills for period {billing_month} using fixed utility and transactional maintenance rules.")
    print(f"📄 CSV exported: {output_csv} ({len(csv_rows)} tenants)")

except psycopg2.Error as e:
    print(f"❌ Database error: {e}")
    if conn:
        conn.rollback()
except Exception as e:
    print(f"❌ Unexpected script error: {e}")
finally:
    if conn:
        conn.close()