#!/usr/bin/env python3
"""
Ledger Correction Import — Full Overwrite (Maintenance fixed + common_misc final)

Processes CSV/XLSX with columns:
 Plot | Flat | Tenant Name | phone | IS_ACTIVE | Flat No | Prev Month | Curr Month
 | Prev Baki | Prev Jama | Bhadu | Light Bill | Gas Bill | Maintenance | common_misc
 | Month Total | GROSS BAKI | New Baki | RUNNING COLLECTION | COLLECTION DATE

- Curr Month Mon-YY (Jun-26) -> billing_month YYYY-MM (2026-06)
- Maintenance = fixed_maintenance only, common_misc = final shared+unit (additional)
- For combined Jun-Jul file: process Jun then Jul in billing_month ASC,
  compare expected Jul Prev (cascaded from Jun new_baki) vs file Jul Prev,
  if |delta|>0.50 flag diff and insert file Jul Prev (file-wins)
- Dry run: outer ROLLBACK, returns diff
- IMPORTANT: This import does NOT queue any WhatsApp/payment-received notification
  (no INSERT into tenant_reminders / reminder_send_log, no call to send_due_reminders).
  It only updates monthly_ledgers + tenant_payments (RENT) for historical months
  Jun/Jul, so tenants will NOT receive a notification for these corrections.
"""
import csv
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from db_config import get_db_connection

# Reuse helpers from gui_api (copied for standalone)
def _split_carry(nb):
    nb = float(nb)
    if nb >= 0:
        return nb, 0.00
    return 0.00, -nb

def _sync_ledger_jama(cur, tenant_id, billing_month):
    cur.execute("""
        SELECT COALESCE(SUM(amount), 0.00) AS total_paid,
               COALESCE(to_char(MAX(payment_date), 'DD-MM-YYYY'), '0') AS last_pay_date
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND billing_month = %s AND payment_type = 'RENT'
    """, (tenant_id, billing_month))
    row = cur.fetchone()
    total_paid = float(row['total_paid'])
    if total_paid > 0:
        cur.execute("""
            SELECT COALESCE((SELECT new_baki FROM rental_core.monthly_ledgers
                             WHERE tenant_id = %s AND billing_month < %s
                             ORDER BY billing_month DESC LIMIT 1), 0.00) AS prev_nb
        """, (tenant_id, billing_month))
        prev_nb = float(cur.fetchone()['prev_nb'])
        carry_baki, carry_jama = _split_carry(prev_nb)
        cur.execute("""
            INSERT INTO rental_core.monthly_ledgers
                (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied,
                 light_bill, gas_bill, maintenance_applied, common_misc_expenses)
            SELECT t.room_id, t.id, %s, %s, %s, 0.00, 0.00, 0.00, 0.00, 0.00
            FROM rental_core.tenants t WHERE t.id = %s
            ON CONFLICT (tenant_id, billing_month) DO NOTHING
        """, (billing_month, carry_baki, carry_jama, tenant_id))
    cur.execute("""
        UPDATE rental_core.monthly_ledgers
        SET jama = %s,
            jama_date = %s,
            payment_status = CASE
                WHEN gross_baki <= 0 THEN 'Paid'
                WHEN %s >= gross_baki THEN 'Paid'
                WHEN %s > 0 THEN 'Partial'
                ELSE 'Unpaid'
            END
        WHERE tenant_id = %s AND billing_month = %s
    """, (total_paid, row['last_pay_date'], total_paid, total_paid,
          tenant_id, billing_month))
    if total_paid <= 0:
        cur.execute("""
            DELETE FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
              AND bhadu_applied = 0 AND light_bill = 0 AND gas_bill = 0
              AND maintenance_applied = 0 AND common_misc_expenses = 0
              AND jama = 0;
        """, (tenant_id, billing_month))
    _cascade_ledger_chain(cur, tenant_id, billing_month)

def _cascade_ledger_chain(cur, tenant_id, from_month):
    cur.execute("""
        SELECT billing_month FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month > %s
        ORDER BY billing_month ASC
    """, (tenant_id, from_month))
    months = [r['billing_month'] for r in cur.fetchall()]
    if not months:
        return
    cur.execute("""
        SELECT COALESCE(new_baki, 0.00) AS new_baki
        FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month = %s
    """, (tenant_id, from_month))
    base = cur.fetchone()
    if base is None:
        return
    next_prev_baki, next_prev_jama = _split_carry(float(base['new_baki']))
    for bm in months:
        cur.execute("""
            UPDATE rental_core.monthly_ledgers
            SET prev_baki = %s, prev_jama = %s
            WHERE tenant_id = %s AND billing_month = %s
            RETURNING COALESCE(new_baki, 0.00) AS new_baki
        """, (next_prev_baki, next_prev_jama, tenant_id, bm))
        row = cur.fetchone()
        if row:
            next_prev_baki, next_prev_jama = _split_carry(float(row['new_baki']))

def _parse_mon_yy(s):
    s = str(s).strip()
    # Jun-26, Jun-2026, Jun 26, etc.
    for fmt in ("%b-%y", "%b-%Y", "%b %y", "%b %Y", "%b-%y ", "%Y-%m"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m")
        except:
            continue
    # Try "Jun-26" -> 2026-06 (assume 20xx for yy<50 else 19xx)
    m = re.match(r"^\s*([A-Za-z]{3})\s*[-/\s]\s*(\d{2,4})\s*$", s)
    if m:
        mon_str, yr_str = m.group(1), m.group(2)
        try:
            mon = datetime.strptime(mon_str, "%b").month
            yr = int(yr_str)
            if yr < 100:
                yr += 2000 if yr < 50 else 1900
            return f"{yr}-{mon:02d}"
        except:
            pass
    # Try YYYY-MM direct
    if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", s):
        return s
    return None

def _parse_amount(s):
    if s is None or str(s).strip() == "":
        return 0.0
    # Handle "3,780.00", "₹ 3,780", "3,780"
    cleaned = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(cleaned) if cleaned not in ("", ".", "-") else 0.0
    except:
        return 0.0

def _parse_date(s, curr_month=None):
    # Ledger is postpaid: billing_month Aug's payment is received in Sep.
    # So day-only "10" or "10.06" for billing_month 2026-08 must become 10-09-2026 (next month), not 10-08-2026.
    if s is None or str(s).strip() in ("", "0", "0.0"):
        return "0"
    s = str(s).strip()
    # Helper to get next month from curr_month (billing_month)
    def _next_month(curr):
        try:
            cm = datetime.strptime(curr, "%Y-%m")
            # first day of next month
            if cm.month == 12:
                return datetime(cm.year + 1, 1, 1)
            else:
                return datetime(cm.year, cm.month + 1, 1)
        except:
            return None
    # Try full date with year first (e.g., 10-09-2026, 2026-09-10) — return as is, no shift
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%d-%m-%Y")
        except:
            continue
    # Try month-day without year (e.g., 10.06, 10-06) — assume next month's year/month
    for fmt in ("%d.%m", "%d-%m", "%d/%m"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900 and curr_month:
                nm = _next_month(curr_month)
                if nm:
                    dt = dt.replace(year=nm.year, month=nm.month)
                else:
                    try:
                        cm = datetime.strptime(curr_month, "%Y-%m")
                        dt = dt.replace(year=cm.year)
                    except:
                        pass
            return dt.strftime("%d-%m-%Y")
        except:
            continue
    # Handle "10.00", "12", "10" as day only (10th of next month)
    m = re.match(r"^\s*(\d{1,2})\s*\.?\s*0*\.?\s*$", s)
    if m and curr_month:
        day = int(m.group(1))
        nm = _next_month(curr_month)
        if nm:
            try:
                return datetime(nm.year, nm.month, day).strftime("%d-%m-%Y")
            except:
                pass
        # Fallback to curr_month if next month fails
        try:
            cm = datetime.strptime(curr_month, "%Y-%m")
            return datetime(cm.year, cm.month, day).strftime("%d-%m-%Y")
        except:
            pass
    # Fallback: day only with different pattern
    m2 = re.match(r"^\s*(\d{1,2})\s*$", s)
    if m2 and curr_month:
        day = int(m2.group(1))
        nm = _next_month(curr_month)
        if nm:
            try:
                return datetime(nm.year, nm.month, day).strftime("%d-%m-%Y")
            except:
                pass
    if re.match(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", s):
        return s.replace("/", "-")
    return "0"

def _lookup_tenant(cur, phone, flat_no, tenant_name):
    # Flat-wise priority: same tenant can occupy multiple units (e.g., KARAN 56/2 and 57/1 from Aug)
    # Must treat same phone with different Flat No as different tenants/ledgers.
    if flat_no:
        flat_no = str(flat_no).strip()
        cur.execute("SELECT t.id, t.room_id, t.tenant_name FROM rental_core.tenants t JOIN rental_core.rooms r ON t.room_id=r.id WHERE r.display_address = %s AND t.is_active='Y' LIMIT 1", (flat_no,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute("SELECT t.id, t.room_id, t.tenant_name FROM rental_core.tenants t JOIN rental_core.rooms r ON t.room_id=r.id WHERE regexp_replace(r.display_address, '[^0-9A-Za-z/]', '', 'g') = %s AND t.is_active='Y' LIMIT 1", (re.sub(r"[^0-9A-Za-z/]", "", flat_no),))
        row = cur.fetchone()
        if row:
            return row
        # Flat exists but vacant -> do NOT fallback to phone (would incorrectly map KARAN 57/1 to 56/2)
        # Check if room exists at all (vacant)
        cur.execute("SELECT id FROM rental_core.rooms WHERE display_address = %s LIMIT 1", (flat_no,))
        if cur.fetchone():
            return None
        cur.execute("SELECT id FROM rental_core.rooms WHERE regexp_replace(display_address, '[^0-9A-Za-z/]', '', 'g') = %s LIMIT 1", (re.sub(r"[^0-9A-Za-z/]", "", flat_no),))
        if cur.fetchone():
            return None
    # Fallback: phone or name only if flat not provided or room not found
    phone_digits = re.sub(r"\D", "", str(phone or ""))
    if phone_digits:
        last10 = phone_digits[-10:]
        cur.execute("SELECT id, room_id, tenant_name FROM rental_core.tenants WHERE regexp_replace(phone_number, '[^0-9]', '', 'g') LIKE %s AND is_active='Y' LIMIT 1", (f"%{last10}",))
        row = cur.fetchone()
        if row:
            return row
    if tenant_name:
        cur.execute("SELECT id, room_id, tenant_name FROM rental_core.tenants WHERE tenant_name ILIKE %s AND is_active='Y' LIMIT 1", (f"%{str(tenant_name).strip()}%",))
        row = cur.fetchone()
        if row:
            return row
    return None

def _create_tenant(cur, flat_no, tenant_name, phone, bhadu, maintenance, billing_month):
    """Auto-create tenant for first occurrence (vacant room) using file data.
    Uses file's Bhadu as base_rent, Maintenance as maintenance, property default for security_deposit.
    flat_no like '56 / 16' must map to existing room; if not found, create room under Plot.
    Returns new tenant row (id, room_id, tenant_name) or None if cannot.
    """
    flat_no = str(flat_no or "").strip()
    tenant_name = str(tenant_name or "").strip() or "Unknown"
    phone = re.sub(r"\D", "", str(phone or ""))[-10:] or "0000000000"
    # Find room by display_address
    cur.execute("SELECT id, master_id, display_address FROM rental_core.rooms WHERE display_address = %s LIMIT 1", (flat_no,))
    room = cur.fetchone()
    if not room and flat_no:
        # Try without spaces
        cur.execute("SELECT id, master_id, display_address FROM rental_core.rooms WHERE regexp_replace(display_address, '[^0-9A-Za-z/]', '', 'g') = %s LIMIT 1", (re.sub(r"[^0-9A-Za-z/]", "", flat_no),))
        room = cur.fetchone()
    if not room:
        # Fallback: try to find property by Plot and create room
        # Plot is not passed here; caller should handle room creation via Flat No only
        return None
    room_id = room["id"]
    master_id = room["master_id"]
    # Check if room already has active tenant (should be vacant per prior check)
    cur.execute("SELECT id FROM rental_core.tenants WHERE room_id=%s AND is_active='Y' LIMIT 1", (room_id,))
    if cur.fetchone():
        return None
    # Get property defaults for security_deposit
    cur.execute("SELECT default_base_rent, default_maintenance_fee, default_security_deposit FROM rental_core.property_masters WHERE id=%s", (master_id,))
    prop = cur.fetchone()
    base_rent = float(bhadu) if bhadu and float(bhadu) > 0 else float(prop["default_base_rent"] or 3300) if prop else 3300
    maint = float(maintenance) if maintenance is not None and str(maintenance).strip() != "" else float(prop["default_maintenance_fee"] or 0) if prop else 0
    sec_dep = float(prop["default_security_deposit"] or 5000) if prop else 5000
    # Lease start: first day of billing_month
    try:
        lease_start = datetime.strptime(billing_month, "%Y-%m").date().replace(day=1)
    except:
        lease_start = datetime.now().date()
    cur.execute("""
        INSERT INTO rental_core.tenants
            (room_id, tenant_name, phone_number, base_rent_applied, maintenance_applied, security_deposit_applied, is_active, lease_start_date)
        VALUES (%s, %s, %s, %s, %s, %s, 'Y', %s)
        RETURNING id, room_id, tenant_name
    """, (room_id, tenant_name, phone, base_rent, maint, sec_dep, lease_start))
    new_row = cur.fetchone()
    return new_row

def process_file(file_path, dry_run=True, payment_mode="overwrite"):
    import pandas as pd

    # Detect file type
    p = Path(file_path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(file_path, dtype=str).fillna("")
        # Also handle .xls via xlrd if needed
    else:
        # Try csv with various encodings
        try:
            df = pd.read_csv(file_path, dtype=str).fillna("")
        except:
            df = pd.read_csv(file_path, dtype=str, encoding='latin1').fillna("")

    # Normalize headers: strip, handle BOM, quotes, and map case-insensitive
    # Handles files where header was saved with single quotes e.g. "'Plot'" or extra spaces/tabs
    df.columns = [re.sub(r"\s+", " ", str(c).strip().replace("\ufeff","").strip("'\"").strip()) for c in df.columns]
    # If file has unnamed extra columns due to trailing tabs (e.g., "Unnamed: 19"), drop them
    df = df.loc[:, ~df.columns.str.contains("^Unnamed", case=False, na=False)]
    # Map expected columns (flexible, handles "common_misc " with trailing space, quotes)
    col_map = {}
    for c in df.columns:
        lc = c.lower().replace("_", " ").strip().strip("'\"").strip()
        # Normalize: collapse multiple spaces, remove trailing spaces
        lc = re.sub(r"\s+", " ", lc)
        if lc in ("plot", "plot no", "plot number"):
            col_map["plot"] = c
        elif lc in ("flat",):
            col_map["flat"] = c
        elif lc in ("tenant name", "tenant", "name"):
            col_map["tenant_name"] = c
        elif lc in ("phone", "mobile", "phone number"):
            col_map["phone"] = c
        elif lc in ("is_active", "is active", "active"):
            col_map["is_active"] = c
        elif lc in ("flat no", "flat_no", "display_address", "flatno"):
            col_map["flat_no"] = c
        elif lc in ("prev month", "prev_month"):
            col_map["prev_month"] = c
        elif lc in ("curr month", "curr_month", "currmonth", "billing month", "billing_month"):
            col_map["curr_month"] = c
        elif lc in ("prev baki", "prev_baki", "prev baki ", "prevbaki"):
            col_map["prev_baki"] = c
        elif lc in ("prev jama", "prev_jama", "prevjama"):
            col_map["prev_jama"] = c
        elif lc in ("bhadu", "bhadu applied", "rent"):
            col_map["bhadu"] = c
        elif lc == "light bill" or lc == "light_bill":
            col_map["light_bill"] = c
        elif lc == "gas bill" or lc == "gas_bill":
            col_map["gas_bill"] = c
        elif lc == "maintenance":
            col_map["maintenance"] = c
        elif lc in ("common_misc", "common misc", "common misc expenses", "common_misc_expenses"):
            col_map["common_misc"] = c
        elif lc in ("month total", "month_total", "monthtotal"):
            col_map["month_total"] = c
        elif lc in ("gross baki", "gross_baki", "gross baki ", "gross"):
            col_map["gross_baki"] = c
        elif lc in ("new baki", "new_baki", "new baki ", "newbaki"):
            col_map["new_baki"] = c
        elif lc in ("running collection", "running_collection", "collection", "jama", "collection amount", "running collection ", "running_collection "):
            col_map["jama"] = c
        elif lc in ("collection date", "collection_date", "jama_date", "jama date", "date", "collection date "):
            col_map["jama_date"] = c

    required = ["curr_month", "prev_baki", "prev_jama", "bhadu", "light_bill", "gas_bill", "maintenance", "common_misc"]
    missing = [k for k in required if k not in col_map]
    if missing:
        # common_misc may be missing in old files — allow but warn
        if "common_misc" in missing:
            # If missing, treat as 0
            pass
        else:
            raise ValueError(f"Missing required columns: {missing}. Found: {list(df.columns)}")

    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    # For dry_run we will rollback at end
    report = {"total": 0, "success": 0, "failures": [], "diffs": [], "months_affected": set(), "details": []}

    # Group rows by billing_month for ordered processing (Jun before Jul)
    rows_by_month = {}
    for idx, row in df.iterrows():
        # Skip empty tenant rows
        tenant_name = str(row.get(col_map.get("tenant_name", ""), "")).strip()
        phone = str(row.get(col_map.get("phone", ""), "")).strip()
        flat_no = str(row.get(col_map.get("flat_no", ""), "")).strip()
        if not tenant_name and not phone and not flat_no:
            continue
        is_active = str(row.get(col_map.get("is_active", ""), "Y")).strip().upper()
        if is_active == "N":
            # Skip inactive rows — they are placeholders (e.g., duplicate KARAN 57/2 N) and should not overwrite active ledger
            continue
        curr_month_raw = str(row.get(col_map.get("curr_month", ""), "")).strip()
        billing_month = _parse_mon_yy(curr_month_raw)
        if not billing_month:
            report["failures"].append({"row": idx+2, "tenant": tenant_name or phone, "reason": f"Invalid Curr Month '{curr_month_raw}'"})
            continue
        rows_by_month.setdefault(billing_month, []).append((idx, row))

    sorted_months = sorted(rows_by_month.keys())

    # For file-wins Jul logic, we need to track expected Jul Prev per tenant
    expected_jul_prev = {}  # tenant_id -> (exp_baki, exp_jama)
    processed_tenants = set()

    try:
        for billing_month in sorted_months:
            for idx, row in rows_by_month[billing_month]:
                report["total"] += 1
                tenant_name = str(row.get(col_map.get("tenant_name", ""), "")).strip()
                phone = str(row.get(col_map.get("phone", ""), "")).strip()
                flat_no = str(row.get(col_map.get("flat_no", ""), "")).strip()
                try:
                    cur.execute("SAVEPOINT sp_row")
                    tenant = _lookup_tenant(cur, phone, flat_no, tenant_name)
                    if not tenant:
                        # Auto-create tenant for first occurrence in file (vacant room, first month)
                        bhadu_tmp = _parse_amount(row.get(col_map.get("bhadu", ""), 0))
                        maint_tmp = _parse_amount(row.get(col_map.get("maintenance", ""), 0))
                        created = _create_tenant(cur, flat_no, tenant_name, phone, bhadu_tmp, maint_tmp, billing_month)
                        if not created:
                            raise ValueError(f"Tenant not found and auto-create failed (phone={phone}, flat={flat_no}, name={tenant_name}) — room may not exist or already occupied")
                        tenant = created
                        tenant["_auto_created"] = True
                    else:
                        # Flat-wise tenant found — check flat+name+phone combination per user spec:
                        # - Flat + Name match but mobile differs -> same tenant (phone update)
                        # - Flat + Phone match but name differs -> same tenant (name update)
                        # - Flat matches but Name & Phone both differ -> different tenant for same flat (tenancy change, e.g., PRAMOD BHAI -> SAMIR for 57/7)
                        file_phone_digits = re.sub(r"\D", "", str(phone or ""))[-10:] if phone else ""
                        cur.execute("SELECT tenant_name, phone_number, room_id FROM rental_core.tenants WHERE id=%s", (tenant["id"],))
                        db_tenant = cur.fetchone()
                        if db_tenant:
                            db_name_norm = (db_tenant["tenant_name"] or "").strip().lower()
                            file_name_norm = str(tenant_name or "").strip().lower()
                            db_phone_digits = re.sub(r"\D", "", str(db_tenant["phone_number"] or ""))[-10:]
                            name_match = (db_name_norm == file_name_norm) and file_name_norm != ""
                            phone_match = (db_phone_digits == file_phone_digits) and file_phone_digits != ""
                            flat_match = True  # we found via flat, so flat matches
                            # Determine same vs different
                            if flat_match and name_match and not phone_match:
                                # Same flat+name, phone updated -> same tenant, update phone, warn
                                report["diffs"].append({"tenant": tenant_name, "flat": flat_no, "month": billing_month, "kind": "Mobile differed", "expected": {"phone": db_tenant["phone_number"]}, "file": {"phone": phone}, "warning": f"Mobile differed actual {db_tenant['phone_number']} / file {phone} for flat {flat_no}"})
                                cur.execute("UPDATE rental_core.tenants SET phone_number=%s WHERE id=%s", (file_phone_digits or db_tenant["phone_number"], tenant["id"]))
                            elif flat_match and phone_match and not name_match:
                                # Same flat+phone, name differs -> same tenant, update name, warn
                                report["diffs"].append({"tenant": tenant_name, "flat": flat_no, "month": billing_month, "kind": "Name differed", "expected": {"name": db_tenant["tenant_name"]}, "file": {"name": tenant_name}, "warning": f"Name differed actual '{db_tenant['tenant_name']}' / file '{tenant_name}' for flat {flat_no} phone {phone}"})
                                cur.execute("UPDATE rental_core.tenants SET tenant_name=%s WHERE id=%s", (tenant_name, tenant["id"]))
                            elif flat_match and not name_match and not phone_match:
                                # Flat same but both name and phone differ -> different tenant for same flat (tenancy change, e.g., 57/7 PRAMOD -> SAMIR)
                                # Also handles multi-unit where same tenant occupies two flats (KARAN 56/2 and 57/1 same name/phone but different flat -> same tenant multi-unit, handled below)
                                report["diffs"].append({"tenant": tenant_name, "flat": flat_no, "month": billing_month, "kind": "Tenancy change", "expected": {"name": db_tenant["tenant_name"], "phone": db_tenant["phone_number"]}, "file": {"name": tenant_name, "phone": phone}, "warning": f"Tenancy change for flat {flat_no}: actual '{db_tenant['tenant_name']}' ({db_tenant['phone_number']}) -> file '{tenant_name}' ({phone})"})
                                # Check if this is actually multi-unit same tenant (same name+phone already has active tenant in another room)
                                cur.execute("SELECT id, room_id FROM rental_core.tenants WHERE regexp_replace(phone_number,'[^0-9]','','g') LIKE %s AND lower(tenant_name)=%s AND is_active='Y' AND room_id != %s LIMIT 1", (f"%{file_phone_digits}", file_name_norm, tenant["room_id"]))
                                same_tenant_other_room = cur.fetchone()
                                if same_tenant_other_room:
                                    # Same tenant (same name+phone) already active in another flat -> this is additional unit, not a replacement
                                    # Keep old tenant for previous flat, create new tenant for this flat (multi-unit)
                                    # Deactivate old tenant for this flat only if it was different person, but here old is different person, so deactivate old and create new for this flat
                                    try:
                                        prev_month_end = (datetime.strptime(billing_month, "%Y-%m").date().replace(day=1) - timedelta(days=1))
                                    except:
                                        prev_month_end = None
                                    cur.execute("UPDATE rental_core.tenants SET is_active='N', lease_end_date=%s WHERE id=%s", (prev_month_end, tenant["id"]))
                                    cur.execute("DELETE FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month >= %s", (tenant["id"], billing_month))
                                    cur.execute("DELETE FROM rental_core.tenant_payments WHERE tenant_id=%s AND billing_month >= %s", (tenant["id"], billing_month))
                                    bhadu_tmp2 = _parse_amount(row.get(col_map.get("bhadu", ""), 0))
                                    maint_tmp2 = _parse_amount(row.get(col_map.get("maintenance", ""), 0))
                                    created2 = _create_tenant(cur, flat_no, tenant_name, phone, bhadu_tmp2, maint_tmp2, billing_month)
                                    if created2:
                                        tenant = created2
                                        tenant["_auto_created"] = True
                                    else:
                                        cur.execute("UPDATE rental_core.tenants SET is_active='Y', lease_end_date=NULL WHERE id=%s", (tenant["id"],))
                                else:
                                    # Pure tenancy change for this flat (e.g., 57/7 PRAMOD -> SAMIR)
                                    cur.execute("SELECT COUNT(*) AS cnt FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month < %s", (tenant["id"], billing_month))
                                    has_prior = cur.fetchone()["cnt"] > 0
                                    if has_prior:
                                        try:
                                            prev_month_end = (datetime.strptime(billing_month, "%Y-%m").date().replace(day=1) - timedelta(days=1))
                                        except:
                                            prev_month_end = None
                                        cur.execute("UPDATE rental_core.tenants SET is_active='N', lease_end_date=%s WHERE id=%s", (prev_month_end, tenant["id"]))
                                        cur.execute("DELETE FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month >= %s", (tenant["id"], billing_month))
                                        cur.execute("DELETE FROM rental_core.tenant_payments WHERE tenant_id=%s AND billing_month >= %s", (tenant["id"], billing_month))
                                        bhadu_tmp2 = _parse_amount(row.get(col_map.get("bhadu", ""), 0))
                                        maint_tmp2 = _parse_amount(row.get(col_map.get("maintenance", ""), 0))
                                        created2 = _create_tenant(cur, flat_no, tenant_name, phone, bhadu_tmp2, maint_tmp2, billing_month)
                                        if created2:
                                            tenant = created2
                                            tenant["_auto_created"] = True
                                        else:
                                            cur.execute("UPDATE rental_core.tenants SET is_active='Y', lease_end_date=NULL WHERE id=%s", (tenant["id"],))
                                    else:
                                        cur.execute("UPDATE rental_core.tenants SET tenant_name=%s, phone_number=%s WHERE id=%s", (tenant_name, file_phone_digits or db_tenant["phone_number"], tenant["id"]))
                            else:
                                # Flat, name, phone all match -> same tenant, no warnings, proceed to ledger update
                                pass
                    tenant_id = tenant["id"]
                    room_id = tenant["room_id"]

                    # Parse amounts
                    prev_baki = _parse_amount(row.get(col_map.get("prev_baki", ""), 0))
                    prev_jama = _parse_amount(row.get(col_map.get("prev_jama", ""), 0))
                    bhadu = _parse_amount(row.get(col_map.get("bhadu", ""), 0))
                    light_bill = _parse_amount(row.get(col_map.get("light_bill", ""), 0))
                    gas_bill = _parse_amount(row.get(col_map.get("gas_bill", ""), 0))
                    maintenance = _parse_amount(row.get(col_map.get("maintenance", ""), 0))
                    common_misc = _parse_amount(row.get(col_map.get("common_misc", ""), 0)) if "common_misc" in col_map else 0.0
                    jama = _parse_amount(row.get(col_map.get("jama", ""), 0))
                    jama_date_raw = str(row.get(col_map.get("jama_date", ""), "")).strip()
                    jama_date = _parse_date(jama_date_raw, billing_month)

                    # Validate month_total, gross, new_baki if present
                    month_total_file = _parse_amount(row.get(col_map.get("month_total", ""), 0)) if "month_total" in col_map else None
                    gross_file = _parse_amount(row.get(col_map.get("gross_baki", ""), 0)) if "gross_baki" in col_map else None
                    new_baki_file = _parse_amount(row.get(col_map.get("new_baki", ""), 0)) if "new_baki" in col_map else None

                    month_total_calc = bhadu + light_bill + gas_bill + maintenance + common_misc
                    gross_calc = prev_baki - prev_jama + month_total_calc
                    new_baki_calc = gross_calc - jama

                    if month_total_file is not None and month_total_file != 0 and abs(month_total_file - month_total_calc) > 0.50:
                        raise ValueError(f"Month Total mismatch: file {month_total_file} vs calc {month_total_calc:.2f} (Bhadu+Light+Gas+Maintenance+common_misc)")
                    if gross_file is not None and gross_file != 0 and abs(gross_file - gross_calc) > 0.50:
                        # Soft warning — file's GROSS may be before collection, keep file Prev but log diff
                        report["diffs"].append({
                            "tenant": tenant_name or f"ID {tenant_id}" if 'tenant_id' in locals() else (tenant_name or "unknown"),
                            "flat": flat_no,
                            "month": billing_month,
                            "kind": "GROSS mismatch",
                            "expected": {"gross": gross_calc},
                            "file": {"gross": gross_file},
                            "delta": round(gross_file - gross_calc, 2),
                        })
                    if new_baki_file is not None and new_baki_file != 0 and abs(new_baki_file - new_baki_calc) > 0.50:
                        # File's New Baki often equals GROSS (before RUNNING COLLECTION) — log diff but don't fail, DB will compute New Baki = GROSS - jama
                        report["diffs"].append({
                            "tenant": tenant_name or f"ID {tenant_id}" if 'tenant_id' in locals() else (tenant_name or "unknown"),
                            "flat": flat_no,
                            "month": billing_month,
                            "kind": "New Baki mismatch",
                            "expected": {"new_baki": new_baki_calc},
                            "file": {"new_baki": new_baki_file},
                            "delta": round(new_baki_file - new_baki_calc, 2),
                        })

                    # For file-wins: compare each month's Prev (except first) against expected from previous month's new_baki
                    if billing_month not in report["months_affected"]:
                        report["months_affected"].add(billing_month)
                    if tenant_id in expected_jul_prev and billing_month != sorted_months[0]:
                        exp_baki, exp_jama = expected_jul_prev[tenant_id]
                        if abs(exp_baki - prev_baki) > 0.50 or abs(exp_jama - prev_jama) > 0.50:
                            report["diffs"].append({
                                "tenant": tenant_name or f"ID {tenant_id}",
                                "flat": flat_no,
                                "month": billing_month,
                                "expected": {"prev_baki": exp_baki, "prev_jama": exp_jama},
                                "file": {"prev_baki": prev_baki, "prev_jama": prev_jama},
                                "delta_baki": round(prev_baki - exp_baki, 2),
                                "delta_jama": round(prev_jama - exp_jama, 2),
                            })
                            # file-wins: keep file values (already parsed)
                    # For 3-month case, also check Aug diff (when sorted_months has 3: Jun,Jul,Aug, Aug is last)
                    # The above already handles last month (Aug) vs expected from Jul (since expected_jul_prev is from previous month's new_baki)
                    # But expected_jul_prev is overwritten each month, so for Aug it will be from Jul, correct

                    # Capture old values for diff
                    cur.execute("SELECT prev_baki, prev_jama, bhadu_applied, light_bill, gas_bill, maintenance_applied, common_misc_expenses, jama FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_id, billing_month))
                    old = cur.fetchone()
                    old_vals = dict(old) if old else None

                    # Handle payment overwrite
                    if payment_mode == "overwrite":
                        cur.execute("DELETE FROM rental_core.tenant_payments WHERE tenant_id=%s AND billing_month=%s AND payment_type='RENT'", (tenant_id, billing_month))
                    if jama > 0 and jama_date != "0":
                        # Parse jama_date to date
                        try:
                            j_date = datetime.strptime(jama_date, "%d-%m-%Y").date()
                        except:
                            j_date = datetime.now().date()
                        cur.execute("SELECT COALESCE(MAX(batch_id),0)+1 AS nb FROM rental_core.tenant_payments")
                        batch_id = cur.fetchone()["nb"]
                        cur.execute("""
                            INSERT INTO rental_core.tenant_payments (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, batch_id)
                            VALUES (%s, %s, %s, %s, %s, 'CASH', 'RENT', %s)
                        """, (tenant_id, room_id, billing_month, j_date, jama, batch_id))
                    elif jama == 0:
                        # No payment, ensure jama cleared via sync
                        pass

                    # UPSERT monthly_ledgers with exact file values
                    cur.execute("""
                        INSERT INTO rental_core.monthly_ledgers
                            (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied, light_bill, gas_bill, maintenance_applied, common_misc_expenses)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, billing_month) DO UPDATE SET
                            prev_baki = EXCLUDED.prev_baki,
                            prev_jama = EXCLUDED.prev_jama,
                            bhadu_applied = EXCLUDED.bhadu_applied,
                            light_bill = EXCLUDED.light_bill,
                            gas_bill = EXCLUDED.gas_bill,
                            maintenance_applied = EXCLUDED.maintenance_applied,
                            common_misc_expenses = EXCLUDED.common_misc_expenses
                    """, (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu, light_bill, gas_bill, maintenance, common_misc))

                    # Sync jama
                    _sync_ledger_jama(cur, tenant_id, billing_month)

                    # After Jun, compute expected Jul Prev for file-wins check (store for next month)
                    # We need to compute new_baki for this billing_month after sync
                    cur.execute("SELECT new_baki FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_id, billing_month))
                    nb_row = cur.fetchone()
                    if nb_row:
                        exp_baki, exp_jama = _split_carry(float(nb_row["new_baki"]))
                        # Store for next month (Jul) - will be used when processing Jul rows
                        expected_jul_prev[tenant_id] = (exp_baki, exp_jama)

                    # For single-month file, cascade to future months (Jul/Aug)
                    # But if file contains Jul, we already handle via file-wins, so only cascade if this is the last month in file
                    if billing_month == sorted_months[-1] and len(sorted_months) == 1:
                        _cascade_ledger_chain(cur, tenant_id, billing_month)

                    cur.execute("RELEASE SAVEPOINT sp_row")

                    # Diff for report
                    new_vals = {"prev_baki": prev_baki, "prev_jama": prev_jama, "bhadu": bhadu, "jama": jama}
                    if old_vals:
                        diff_str = f"prev_baki {old_vals['prev_baki']}→{prev_baki}, jama {old_vals['jama']}→{jama}"
                    else:
                        diff_str = f"new ledger for {billing_month}"
                    report["details"].append({"tenant": tenant_name, "flat": flat_no, "month": billing_month, "diff": diff_str})
                    report["success"] += 1
                    processed_tenants.add(tenant_id)

                except Exception as e:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_row")
                    except:
                        pass
                    report["failures"].append({"row": idx+2, "tenant": tenant_name or phone or flat_no, "month": billing_month if 'billing_month' in locals() else "?", "reason": str(e)})
                    continue

        # After processing all months, if combined file, ensure cascades for last month's future months
        if len(sorted_months) > 1:
            last_month = sorted_months[-1]
            for tenant_id in processed_tenants:
                try:
                    _cascade_ledger_chain(cur, tenant_id, last_month)
                except:
                    pass

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        # Convert set to list for JSON
        report["months_affected"] = sorted(list(report["months_affected"]))
        return report

    except Exception as e:
        conn.rollback()
        raise
    finally:
        try:
            cur.close()
            conn.close()
        except:
            pass

def generate_template(month):
    """Generate prefilled CSV template for given month YYYY-MM with 51 tenants"""
    conn = get_db_connection(as_dict=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT t.tenant_name, t.phone_number, r.display_address, pm.plot_number, r.flat_number,
               ml.prev_baki, ml.prev_jama, ml.bhadu_applied, ml.light_bill, ml.gas_bill,
               ml.maintenance_applied, ml.common_misc_expenses, ml.jama, ml.billing_month
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id=r.id
        JOIN rental_core.property_masters pm ON r.master_id=pm.id
        LEFT JOIN rental_core.monthly_ledgers ml ON ml.tenant_id=t.id AND ml.billing_month=%s
        WHERE t.is_active='Y'
        ORDER BY pm.plot_number, r.flat_number
    """, (month,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    import pandas as pd
    data = []
    for r in rows:
        prev_month = (datetime.strptime(month, "%Y-%m") - timedelta(days=1)).strftime("%b-%y")
        curr_month_label = datetime.strptime(month, "%Y-%m").strftime("%b-%y")
        data.append({
            "Plot": r["plot_number"],
            "Flat": r["flat_number"],
            "Tenant Name": r["tenant_name"],
            "phone": r["phone_number"],
            "IS_ACTIVE": "Y",
            "Flat No": r["display_address"],
            "Prev Month": prev_month,
            "Curr Month": curr_month_label,
            "Prev Baki": float(r["prev_baki"] or 0) if r["prev_baki"] is not None else 0,
            "Prev Jama": float(r["prev_jama"] or 0) if r["prev_jama"] is not None else 0,
            "Bhadu": float(r["bhadu_applied"] or 0) if r["bhadu_applied"] is not None else 0,
            "Light Bill": float(r["light_bill"] or 0) if r["light_bill"] is not None else 0,
            "Gas Bill": float(r["gas_bill"] or 0) if r["gas_bill"] is not None else 0,
            "Maintenance": float(r["maintenance_applied"] or 0) if r["maintenance_applied"] is not None else 0,
            "common_misc": float(r["common_misc_expenses"] or 0) if r["common_misc_expenses"] is not None else 0,
            "Month Total": "",
            "GROSS BAKI": "",
            "New Baki": "",
            "RUNNING COLLECTION": float(r["jama"] or 0) if r["jama"] is not None else 0,
            "COLLECTION DATE": "",
        })
    df = pd.DataFrame(data)
    return df
