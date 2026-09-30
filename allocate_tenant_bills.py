import csv
import json
import math
import sys
from datetime import datetime, timedelta
from decimal import Decimal as _Dec
from db_config import get_db_connection

# Half rent mapping for first-time bills not exact half (3300→1700 etc.)
HALF_RENT_MAP = {3300: 1700, 6500: 3400, 7000: 3700, 5000: 2600, 2500: 1350, 3000: 1600, 6400: 3300, 0: 0}

def get_half_rent(base_rent, stored_half=None):
    """Return half rent: prefer stored value, else mapped, else ceil(base/2) to nearest 100."""
    if stored_half is not None:
        try:
            v = float(stored_half)
            if v > 0:
                return v
        except:
            pass
    try:
        br = int(float(base_rent))
        if br in HALF_RENT_MAP:
            return float(HALF_RENT_MAP[br])
    except:
        pass
    try:
        return float(math.ceil(float(base_rent) / 2 / 100) * 100)
    except:
        return float(base_rent) / 2

def is_half_month(lease_start, lease_end, billing_month):
    """True if billing_month is prorated: start after 15th OR end 1st-15th."""
    try:
        bm_year, bm_mon = map(int, billing_month.split('-'))
        if lease_start:
            ls = lease_start
            if isinstance(ls, str):
                ls = datetime.strptime(ls, '%Y-%m-%d').date() if len(ls) >= 10 else None
            if ls and ls.year == bm_year and ls.month == bm_mon and ls.day >= 16:
                return True
        if lease_end:
            le = lease_end
            if isinstance(le, str):
                le = datetime.strptime(le, '%Y-%m-%d').date() if len(le) >= 10 else None
            if le and le.year == bm_year and le.month == bm_mon and 1 <= le.day <= 15:
                return True
    except:
        return False
    return False


# Get billing month in YYYY-MM format (override with --month YYYY-MM).
# Postpaid billing: a bill generated "this month" is actually the PREVIOUS
# month's consumption to be collected, so default to the consumption month
# (previous calendar month) unless explicitly overridden.
def _consumption_month():
    now = datetime.now()
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"

billing_month = _consumption_month()
if "--month" in sys.argv:
    billing_month = sys.argv[sys.argv.index("--month") + 1]
billing_month_display = billing_month

# Calculate the previous month string for rolling over debt (prev_baki)
_bm_dt = datetime.strptime(billing_month, '%Y-%m')
prev_billing_month = (_bm_dt.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')

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
            t.half_rent_applied,
            t.lease_start_date,
            t.lease_end_date,
            r.id AS room_id,
            r.flat_number,
            r.master_id,
            r.display_address,
            r.half_rent AS room_half_rent,
            pm.plot_number,
            pm.default_base_rent,
            pm.default_maintenance_fee,
            pm.default_half_rent
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

    # Per-tenant success/failure report (surfaced to the UI by gui_api)
    report = {"total": 0, "success": 0, "failures": []}

    for (tenant_pk, tenant_name, phone_number, is_active, base_rent_applied, 
         tenant_maintenance_applied, half_rent_applied, lease_start_date, lease_end_date,
         room_id, flat_number, master_id, 
         display_address, room_half_rent, plot_number, default_base_rent, default_maintenance_fee, default_half_rent) in tenants:

        report["total"] += 1
        # Skip months outside lease period — only for future months beyond lease (postpaid)
        # For pre-lease (billing_month < lease_start), do NOT delete existing June for July-start tenants
        # — preserve historical June per ledger correction file (122 rows Jun/Jul/Aug). Only skip creation if no ledger exists.
        # Keep delete only for post-lease (billing_month > lease_end) to avoid Aug ledger for Sep-ended tenancy.
        try:
            _ls_ym = None
            if lease_start_date:
                if hasattr(lease_start_date, 'strftime'):
                    _ls_ym = lease_start_date.strftime('%Y-%m')
                else:
                    _ls_ym = str(lease_start_date)[:7]
            _le_ym = None
            if lease_end_date:
                if hasattr(lease_end_date, 'strftime'):
                    _le_ym = lease_end_date.strftime('%Y-%m')
                else:
                    _le_ym = str(lease_end_date)[:7]
            if _ls_ym and billing_month < _ls_ym:
                # Check if ledger already exists for this month (from correction file) — keep it, don't delete
                cursor.execute("SELECT 1 FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_pk, billing_month))
                if not cursor.fetchone():
                    # No ledger yet and before lease — skip creation (don't create June for Aug-start like Mukesh 35/301)
                    # But don't delete existing June for July-start tenants that already have June (preserve file data)
                    report["success"] += 1
                    continue
                # If ledger exists (e.g., June for July-start from file), fall through to normal update (keep it)
            if _le_ym and billing_month > _le_ym:
                cursor.execute("DELETE FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_pk, billing_month))
                print(f"Skipping {tenant_name} {display_address} for {billing_month} > lease_end {_le_ym} - deleted")
                report["success"] += 1
                continue
        except Exception as _e:
            print(f"Lease check error for {tenant_name}: {_e}")
            pass
        cursor.execute("SAVEPOINT sp_tenant")
        try:
            # 2. Fixed maintenance per unit - added every month
            fixed_maintenance = tenant_maintenance_applied if tenant_maintenance_applied > 0 else default_maintenance_fee

            # 2a. Determine effective Bhadu (rent) for this billing_month — half rent if mid-month start/end
            # Fallback for base 0 (data bug): use property default_base_rent
            try:
                _base_val = float(base_rent_applied) if base_rent_applied is not None else 0
            except:
                _base_val = 0
            effective_base = base_rent_applied if _base_val > 0 else (default_base_rent if default_base_rent is not None else base_rent_applied)
            # Priority for half rent value: tenant.half_rent_applied -> rooms.half_rent -> property.default_half_rent -> computed mapping
            half_rent_val = half_rent_applied or room_half_rent or default_half_rent
            # If half value is still None/0 but base has mapping, get_half_rent will compute from effective_base
            half_rent_computed = _Dec(str(get_half_rent(effective_base, half_rent_val)))
            if is_half_month(lease_start_date, lease_end_date, billing_month):
                bhadu_for_month = half_rent_computed
            else:
                bhadu_for_month = _Dec(str(effective_base))

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
            prev_nb = float(row[0]) if row else 0.00
            if prev_nb >= 0:
                prev_baki = _Dec(prev_nb)
                prev_jama = _Dec(0.00)
            else:
                prev_baki = _Dec(0.00)
                prev_jama = _Dec(-prev_nb)

            # 3b. Get current month's payment (jama) for accurate new_baki on re-runs
            cursor.execute(
                """
                SELECT COALESCE(jama, 0.00) FROM rental_core.monthly_ledgers
                WHERE tenant_id = %s AND billing_month = %s
                """,
                (tenant_pk, billing_month)
            )
            jama_row = cursor.fetchone()
            current_jama = _Dec(jama_row[0]) if jama_row and jama_row[0] is not None else _Dec(0.00)

            # 4. Get room-specific direct utility bills using metrics joins
            # July 2026 torrent amounts are mostly 0 (scrape bug) but last_bill_amt holds correct bill (~588 for 53/401). Fallback to last_bill_amt when amount is 0.
            cursor.execute(
                """
                SELECT 
                    COALESCE(SUM(CASE 
                        WHEN tbm.bill_type = 'NOTICE' 
                            THEN GREATEST(COALESCE(NULLIF(tbm.amount,0), tbm.last_bill_amt, 0) - COALESCE(NULLIF(prev.amount,0), prev.last_bill_amt, 0), 0.00)
                        ELSE COALESCE(NULLIF(tbm.amount,0), tbm.last_bill_amt, 0) END), 0.00),
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
            # Month Total = Bhadu (prorated half if mid-month start/end) + Light Bill + Gas Bill + Maintenance + Misc Expenses
            month_total = bhadu_for_month + light_bill + gas_bill + fixed_maintenance + common_misc_expenses
            
            # Gross Baki = Prev Baki - Prev Jama + Month Total
            gross_baki = prev_baki - prev_jama + month_total
            
            # New Baki = Gross Baki - Jama (current month payment)
            new_baki = gross_baki - current_jama
            
            # For CSV - maintenance value (fixed per-unit monthly maintenance)
            maintenance_for_csv = fixed_maintenance

            # 6. UPSERT into monthly_ledgers — respect manual overrides
            # Actual utility bills go into *_actual columns; effective columns (light_bill etc.) are only overwritten if no manual override exists.
            cursor.execute(
                """
                INSERT INTO rental_core.monthly_ledgers (
                    room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied, 
                    light_bill, light_bill_actual, light_bill_type, gas_bill, gas_bill_actual, maintenance_applied, maintenance_actual, common_misc_expenses, common_misc_actual
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) ON CONFLICT (tenant_id, billing_month) DO UPDATE SET
                    room_id = EXCLUDED.room_id,
                    prev_baki = EXCLUDED.prev_baki,
                    prev_jama = EXCLUDED.prev_jama,
                    bhadu_applied = EXCLUDED.bhadu_applied,
                    light_bill_actual = EXCLUDED.light_bill,
                    gas_bill_actual = EXCLUDED.gas_bill,
                    maintenance_actual = EXCLUDED.maintenance_applied,
                    common_misc_actual = EXCLUDED.common_misc_expenses,
                    light_bill = CASE WHEN monthly_ledgers.light_bill_manual IS NOT NULL THEN monthly_ledgers.light_bill ELSE EXCLUDED.light_bill END,
                    light_bill_type = CASE WHEN monthly_ledgers.light_bill_manual IS NOT NULL THEN monthly_ledgers.light_bill_type ELSE EXCLUDED.light_bill_type END,
                    gas_bill = CASE WHEN monthly_ledgers.gas_bill_manual IS NOT NULL THEN monthly_ledgers.gas_bill ELSE EXCLUDED.gas_bill END,
                    maintenance_applied = CASE WHEN monthly_ledgers.maintenance_manual IS NOT NULL THEN monthly_ledgers.maintenance_applied ELSE EXCLUDED.maintenance_applied END,
                    common_misc_expenses = CASE WHEN monthly_ledgers.common_misc_manual IS NOT NULL THEN monthly_ledgers.common_misc_expenses ELSE EXCLUDED.common_misc_expenses END,
                    manual_override = CASE WHEN monthly_ledgers.light_bill_manual IS NOT NULL OR monthly_ledgers.gas_bill_manual IS NOT NULL OR monthly_ledgers.maintenance_manual IS NOT NULL OR monthly_ledgers.common_misc_manual IS NOT NULL THEN true ELSE false END
                """,
                (
                    room_id, tenant_pk, billing_month, prev_baki, prev_jama, bhadu_for_month,
                    light_bill, light_bill, light_bill_type, gas_bill, gas_bill, fixed_maintenance, fixed_maintenance, common_misc_expenses, common_misc_expenses
                )
            )

            cursor.execute("RELEASE SAVEPOINT sp_tenant")
            report["success"] += 1

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
                f"{bhadu_for_month:.2f}",
                f"{light_bill:.2f}",
                f"{gas_bill:.2f}",
                f"{maintenance_for_csv:.2f}",
                f"{current_jama:.2f}",  # Jama (current month payment)
                "",      # Jama Date
                f"{month_total:.2f}",
                f"{gross_baki:.2f}",
                f"{new_baki:.2f}"
            ])
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"❌ Error for tenant {tenant_name} ({flat_number}): {e}\n{tb}")
            cursor.execute("ROLLBACK TO SAVEPOINT sp_tenant")
            report["failures"].append({"tenant": tenant_name, "flat": flat_number, "reason": str(e)})

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
    print("ALLOC_REPORT " + json.dumps(report))

except psycopg2.Error as e:
    print(f"❌ Database error: {e}")
    if conn:
        conn.rollback()
except Exception as e:
    print(f"❌ Unexpected script error: {e}")
finally:
    if conn:
        conn.close()