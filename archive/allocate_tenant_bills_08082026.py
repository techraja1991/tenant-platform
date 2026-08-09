from datetime import datetime, timedelta
from db_config import get_db_connection

# Get current billing month in YYYY-MM format
billing_month = datetime.now().strftime('%Y-%m')

# Calculate the previous month string for rolling over debt (prev_baki)
prev_month_dt = datetime.now().replace(day=1) - timedelta(days=1)
prev_billing_month = prev_month_dt.strftime('%Y-%m')

conn = None
try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Fetch all active tenants alongside their property configurations
    cursor.execute(
        """
        SELECT 
            t.id AS tenant_pk, 
            t.room_id, 
            r.master_id,
            t.base_rent_applied, 
            pm.default_common_utility
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.is_active = 'Y'
        """
    )
    tenants = cursor.fetchall()

    # Cache for tracking dynamic maintenance distributions per property this month
    # This prevents running the same calculation query repeatedly inside the loop
    property_maintenance_cache = {}

    for tenant_pk, room_id, master_id, base_rent_applied, default_common_utility in tenants:
        
        # 2. Dynamic Maintenance Calculation (Calculate on demand per property)
        if master_id not in property_maintenance_cache:
            # Sum all logged maintenance events for this property this month
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0.00) 
                FROM rental_core.property_maintenance_expenses
                WHERE master_id = %s AND execution_month = %s
                """,
                (master_id, billing_month)
            )
            total_maint_cost = cursor.fetchone()[0]

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
            share = total_maint_cost / tenant_count_for_prop if tenant_count_for_prop > 0 else 0.00
            property_maintenance_cache[master_id] = share

        calculated_maintenance = property_maintenance_cache[master_id]

        # 3. Get previous month's remaining balance (new_baki) to roll over into prev_baki
        cursor.execute(
            """
            SELECT COALESCE(new_baki, 0.00) FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
            """,
            (tenant_pk, prev_billing_month)
        )
        row = cursor.fetchone()
        prev_baki = row[0] if row else 0.00

        # 4. Get room-specific direct utility bills using metrics joins
        cursor.execute(
            """
            SELECT COALESCE(SUM(tbm.amount), 0.00) 
            FROM rental_core.torrent_bill_master tbm
            JOIN rental_core.utility_metrics um ON tbm.service_number = um.service_number
            WHERE um.room_id = %s AND tbm.execution_month = %s
            """,
            (room_id, billing_month)
        )
        light_bill = cursor.fetchone()[0]

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

        # 5. UPSERT into monthly_ledgers
        cursor.execute(
            """
            INSERT INTO rental_core.monthly_ledgers (
                room_id, tenant_id, billing_month, prev_baki, bhadu_applied, 
                light_bill, gas_bill, maintenance_applied, common_utility_share
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (tenant_id, billing_month) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                prev_baki = EXCLUDED.prev_baki,
                bhadu_applied = EXCLUDED.bhadu_applied,
                light_bill = EXCLUDED.light_bill,
                gas_bill = EXCLUDED.gas_bill,
                maintenance_applied = EXCLUDED.maintenance_applied,
                common_utility_share = EXCLUDED.common_utility_share
            """,
            (
                room_id, tenant_pk, billing_month, prev_baki, base_rent_applied,
                light_bill, gas_bill, calculated_maintenance, default_common_utility
            )
        )

    conn.commit()
    print(f"🎉 Successfully allocated bills for period {billing_month} using fixed utility and transactional maintenance rules.")

except psycopg2.Error as e:
    print(f"❌ Database error: {e}")
    if conn:
        conn.rollback()
except Exception as e:
    print(f"❌ Unexpected script error: {e}")
finally:
    if conn:
        conn.close()