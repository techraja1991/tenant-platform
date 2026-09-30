import os
import sys
import subprocess
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from pathlib import Path
from dotenv import load_dotenv
# Load .env explicitly so AUTH_USER/AUTH_PASSWORD apply even if import order changes
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
from db_config import get_db_connection
import whatsapp_message as wm
import reminder_message as rm

app = FastAPI(title="Tenant Management Admin Core API", version="2.0.0")
import base64
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

AUTH_USER = os.getenv("AUTH_USER", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

def current_billing_period():
    """Postpaid billing period: a bill generated 'this month' is actually the
    PREVIOUS month's consumption to be collected. Returns 'YYYY-MM' of the
    consumption (billing) period that the current work should target."""
    now = datetime.now()
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"

class BasicAuthMiddleware(BaseHTTPMiddleware):
    # Paths that must be fetchable without auth (no secrets; needed for PWA/SW)
    PUBLIC_PATHS = ("/sw.js", "/static/icons/", "/static/manifest.webmanifest",
                    "/api/reminders/send-log-screenshot")

    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)
        auth = request.headers.get("Authorization")
        authorized = False
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            except Exception:
                decoded = None
            if decoded:
                user, _, pw = decoded.partition(":")
                authorized = (user == AUTH_USER and pw == AUTH_PASSWORD)
        # NOTE: call_next is deliberately OUTSIDE the try block — endpoint
        # exceptions must propagate as 5xx, never be swallowed into a 401.
        if authorized:
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Tenant Admin"'},
            content={"detail": "Authentication required"},
        )

app.add_middleware(BasicAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    return get_db_connection(as_dict=True)

# --- PYDANTIC SCHEMAS ---
class PropertyCreate(BaseModel):
    apartment_name: str
    plot_number: str
    default_base_rent: Optional[float] = Field(default=3300.00, ge=0)
    default_maintenance_fee: Optional[float] = Field(default=0.00, ge=0)
    default_security_deposit: Optional[float] = Field(default=5000.00, ge=0)
    default_common_utility: Optional[float] = Field(default=0.00, ge=0)

class PropertyUpdate(BaseModel):
    apartment_name: Optional[str] = None
    plot_number: Optional[str] = None
    default_base_rent: Optional[float] = Field(default=None, ge=0)
    default_maintenance_fee: Optional[float] = Field(default=None, ge=0)
    default_security_deposit: Optional[float] = Field(default=None, ge=0)
    default_common_utility: Optional[float] = Field(default=None, ge=0)

class RoomCreate(BaseModel):
    master_id: int
    flat_number: str
    display_address: str

class TenantCreate(BaseModel):
    room_id: int
    tenant_name: str
    phone_number: str
    base_rent_applied: float = Field(ge=0)
    maintenance_applied: Optional[float] = Field(default=0.00, ge=0)
    security_deposit_applied: Optional[float] = Field(default=0.00, ge=0)
    booking_amount_paid: Optional[float] = Field(default=0.00, ge=0)
    pending_deposit_due_date: Optional[str] = Field(default=None)
    lease_start_date: Optional[str] = Field(default=None)
    lease_end_date: Optional[str] = Field(default=None)

class MaintenanceExpenseCreate(BaseModel):
    master_id: int
    expense_name: str
    amount: float = Field(gt=0)
    charge_type: Optional[str] = Field(default="SHARED", pattern="^(SHARED|UNIT|OWNER)$")
    room_id: Optional[int] = None

class PaymentAllocation(BaseModel):
    billing_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    amount: float = Field(gt=0)

class PaymentCreate(BaseModel):
    tenant_id: int
    payment_date: str
    payment_mode: Optional[str] = Field(default="CASH", pattern="^(CASH|UPI|BANK_TRANSFER|CARD|CHEQUE)$")
    payment_type: Optional[str] = Field(default="RENT", pattern="^(RENT|DEPOSIT)$")
    note: Optional[str] = None
    # Option A: single month (backward compatible)
    billing_month: Optional[str] = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    amount: Optional[float] = Field(default=None, gt=0)
    # Option B: split one payment across multiple billing months
    allocations: Optional[List[PaymentAllocation]] = None

class ElectricMappingCreate(BaseModel):
    account_id: int
    room_id: int
    service_number: str
    encrypted_value: str

class GasMappingCreate(BaseModel):
    room_id: int
    customer_number: str


# --- 1. DASHBOARD OVERVIEW ---
@app.get("/api/dashboard/stats")
def get_dashboard_stats():
    conn = get_db()
    cur = conn.cursor()
    execution_month = current_billing_period()

    cur.execute("SELECT COUNT(*) as count FROM rental_core.property_masters;")
    props = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.rooms;")
    total_rooms = cur.fetchone()['count']

    cur.execute("SELECT COUNT(DISTINCT room_id) as count FROM rental_core.tenants WHERE is_active = 'Y';")
    occupied_rooms = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.tenants WHERE is_active = 'Y';")
    active_tenants = cur.fetchone()['count']

    cur.execute("""
        SELECT COALESCE(SUM(new_baki), 0.00) as total_outstanding,
               COALESCE(SUM(bhadu_applied), 0.00) as monthly_rent_income,
               COALESCE(SUM(jama), 0.00) as monthly_collections,
               COALESCE(SUM(prev_baki), 0.00) as carried_forward
        FROM rental_core.monthly_ledgers 
        WHERE billing_month = %s;
    """, (execution_month,))
    led_row = cur.fetchone()
    outstanding = led_row['total_outstanding']
    rent_income = led_row['monthly_rent_income']
    collections = led_row['monthly_collections']
    carried_forward = led_row['carried_forward']

    cur.execute("""
        SELECT COALESCE(SUM(month_total), 0.00) as billed_total
        FROM rental_core.monthly_ledgers
        WHERE billing_month = %s AND month_total > 0;
    """, (execution_month,))
    billed_total = cur.fetchone()['billed_total']

    cur.execute("""
        SELECT COALESCE(SUM(gross_baki), 0.00) AS fixed_due_total,
               COALESCE(SUM(CASE WHEN new_baki > 0 THEN new_baki ELSE 0 END), 0.00) AS current_due_total
        FROM rental_core.monthly_ledgers
        WHERE billing_month = %s;
    """, (execution_month,))
    due_row = cur.fetchone()
    fixed_due_total = due_row['fixed_due_total']
    current_due_total = due_row['current_due_total']

    cur.execute("""
        SELECT pm.apartment_name,
               COALESCE(SUM(ml.new_baki), 0.00) AS outstanding,
               COUNT(ml.id) AS units
        FROM rental_core.monthly_ledgers ml
        JOIN rental_core.rooms r ON ml.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE ml.billing_month = %s
        GROUP BY pm.apartment_name
        ORDER BY outstanding DESC;
    """, (execution_month,))
    outstanding_by_property = cur.fetchall()

    cur.execute("""
        SELECT COALESCE(SUM(new_baki), 0.00) AS older_outstanding
        FROM rental_core.monthly_ledgers
        WHERE billing_month < %s;
    """, (execution_month,))
    older_outstanding = cur.fetchone()['older_outstanding']

    cur.close()
    conn.close()

    vacancy_rate = round((total_rooms - occupied_rooms) / total_rooms * 100, 1) if total_rooms else 0.0
    recovery_pct = round(collections / billed_total * 100, 1) if billed_total else 0.0

    return {
        "execution_month": execution_month,
        "properties": props,
        "rooms": total_rooms,
        "occupied_rooms": occupied_rooms,
        "vacancy_rate": vacancy_rate,
        "active_tenants": active_tenants,
        "total_outstanding_due": float(outstanding),
        "monthly_rent_income": float(rent_income),
        "monthly_collections": float(collections),
        "payment_recovery_pct": recovery_pct,
        "billed_total": float(billed_total),
        "fixed_due_total": float(fixed_due_total),
        "current_due_total": float(current_due_total),
        "carried_forward": float(carried_forward),
        "older_outstanding": float(older_outstanding),
        "outstanding_by_property": [
            {"apartment_name": p['apartment_name'], "outstanding": float(p['outstanding']), "units": p['units']}
            for p in outstanding_by_property
        ]
    }


# --- 1b. REPORT DASHBOARD (monthly + property-monthly aggregates) ---
@app.get("/api/reports/monthly-summary")
def report_monthly_summary():
    """Per-month collection report across all properties, oldest first."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT billing_month,
               COALESCE(SUM(month_total), 0) AS total_billed,
               COALESCE(SUM(gross_baki), 0) AS gross_billed,
               COALESCE(SUM(jama), 0) AS collected,
               COALESCE(SUM(prev_baki), 0) AS initial_pending,
               COALESCE(SUM(prev_baki), 0) - COALESCE(SUM(prev_jama), 0) AS initial_outstanding_net,
               COALESCE(SUM(CASE WHEN new_baki > 0 THEN new_baki ELSE 0 END), 0) AS outstanding_pending,
               COALESCE(SUM(new_baki), 0) AS outstanding_net,
               COUNT(*) AS units
        FROM rental_core.monthly_ledgers
        GROUP BY billing_month
        ORDER BY billing_month ASC;
    """)
    months = []
    for r in cur.fetchall():
        bm = r['billing_month']
        try:
            label = datetime.strptime(bm, "%Y-%m").strftime("%b %Y")
        except Exception:
            label = bm
        total_billed = float(r['total_billed'] or 0)
        gross_billed = float(r['gross_billed'] or 0)
        collected = float(r['collected'] or 0)
        months.append({
            "billing_month": bm,
            "month_label": label,
            "total_billed": total_billed,
            "gross_billed": gross_billed,
            "collected": collected,
            "initial_pending": float(r['initial_pending'] or 0),
            "initial_outstanding_net": float(r['initial_outstanding_net'] or 0),
            "outstanding_pending": float(r['outstanding_pending'] or 0),
            "outstanding_net": float(r['outstanding_net'] or 0),
            "units": r['units'],
            "recovery_pct": round(collected / gross_billed * 100, 1) if gross_billed else 0.0,
            "recovery_on_month_pct": round(collected / total_billed * 100, 1) if total_billed else 0.0,
        })
    cur.close()
    conn.close()
    return {"months": months}


@app.get("/api/reports/property-monthly")
def report_property_monthly():
    """Per-property per-month collection report, oldest first."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT ml.billing_month,
               pm.id AS property_id, pm.apartment_name, pm.plot_number,
               COALESCE(SUM(ml.month_total), 0) AS total_billed,
               COALESCE(SUM(ml.gross_baki), 0) AS gross_billed,
               COALESCE(SUM(ml.jama), 0) AS collected,
               COALESCE(SUM(ml.prev_baki), 0) AS initial_pending,
               COALESCE(SUM(CASE WHEN ml.new_baki > 0 THEN ml.new_baki ELSE 0 END), 0) AS outstanding_pending,
               COALESCE(SUM(ml.new_baki), 0) AS outstanding_net,
               COUNT(*) AS units
        FROM rental_core.monthly_ledgers ml
        JOIN rental_core.rooms r ON ml.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        GROUP BY ml.billing_month, pm.id, pm.apartment_name, pm.plot_number
        ORDER BY ml.billing_month ASC, pm.apartment_name ASC;
    """)
    rows = []
    for r in cur.fetchall():
        bm = r['billing_month']
        try:
            label = datetime.strptime(bm, "%Y-%m").strftime("%b %Y")
        except Exception:
            label = bm
        total_billed = float(r['total_billed'] or 0)
        gross_billed = float(r['gross_billed'] or 0)
        collected = float(r['collected'] or 0)
        rows.append({
            "billing_month": bm,
            "month_label": label,
            "property_id": r['property_id'],
            "apartment_name": r['apartment_name'],
            "plot_number": r['plot_number'],
            "total_billed": total_billed,
            "gross_billed": gross_billed,
            "collected": collected,
            "initial_pending": float(r['initial_pending'] or 0),
            "outstanding_pending": float(r['outstanding_pending'] or 0),
            "outstanding_net": float(r['outstanding_net'] or 0),
            "units": r['units'],
            "recovery_pct": round(collected / gross_billed * 100, 1) if gross_billed else 0.0,
        })
    cur.close()
    conn.close()
    return {"rows": rows}


# --- 2. PROPERTY MASTERS & ROOMS ---
@app.get("/api/dashboard/kpi")
def get_dashboard_kpi():
    """Breakdown data for the clickable KPI cards on the executive dashboard."""
    conn = get_db()
    cur = conn.cursor()
    execution_month = current_billing_period()

    cur.execute("""
        SELECT pm.id AS property_id,
               pm.apartment_name,
               pm.default_base_rent,
               COUNT(r.id) AS total_rooms,
               COUNT(t.id) AS occupied_rooms,
               COALESCE(SUM(ml.bhadu_applied), 0.00) AS rent_billed,
               COALESCE(SUM(ml.jama), 0.00) AS collected,
               COALESCE(SUM(ml.new_baki), 0.00) AS outstanding
        FROM rental_core.property_masters pm
        LEFT JOIN rental_core.rooms r ON r.master_id = pm.id
        LEFT JOIN rental_core.tenants t ON t.room_id = r.id AND t.is_active = 'Y'
        LEFT JOIN rental_core.monthly_ledgers ml
               ON ml.room_id = r.id AND ml.billing_month = %s
        GROUP BY pm.id, pm.apartment_name, pm.default_base_rent
        ORDER BY pm.apartment_name ASC;
    """, (execution_month,))
    properties = cur.fetchall()

    cur.execute("""
        SELECT t.id AS tenant_id,
               t.tenant_name,
               t.phone_number,
               t.is_active,
               r.display_address,
               pm.apartment_name,
               t.base_rent_applied,
               t.security_deposit_applied,
               COALESCE(ml.jama, 0.00) AS collected,
               COALESCE(ml.new_baki, 0.00) AS outstanding
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        LEFT JOIN rental_core.monthly_ledgers ml
               ON ml.tenant_id = t.id AND ml.billing_month = %s
        ORDER BY t.is_active DESC, pm.apartment_name ASC, r.display_address ASC;
    """, (execution_month,))
    tenants = cur.fetchall()

    cur.execute("""
        SELECT t.id AS tenant_id,
               t.tenant_name,
               t.phone_number,
               r.display_address,
               pm.apartment_name,
               ml.gross_baki,
               ml.jama,
               ml.new_baki,
               ml.payment_status,
               ml.prev_baki,
               ml.month_total,
               ml.bhadu_applied,
               ml.light_bill,
               ml.gas_bill,
               ml.maintenance_applied,
               ml.common_misc_expenses
        FROM rental_core.monthly_ledgers ml
        JOIN rental_core.tenants t ON ml.tenant_id = t.id
        JOIN rental_core.rooms r ON ml.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE ml.billing_month = %s
        ORDER BY ml.new_baki DESC;
    """, (execution_month,))
    ledgers = cur.fetchall()

    cur.close()
    conn.close()

    return {
        "execution_month": execution_month,
        "properties": [
            {
                "property_id": p['property_id'],
                "apartment_name": p['apartment_name'],
                "default_base_rent": float(p['default_base_rent'] or 0),
                "total_rooms": p['total_rooms'],
                "occupied_rooms": p['occupied_rooms'],
                "vacant_rooms": p['total_rooms'] - p['occupied_rooms'],
                "rent_billed": float(p['rent_billed']),
                "collected": float(p['collected']),
                "outstanding": float(p['outstanding'])
            }
            for p in properties
        ],
        "tenants": [
            {
                "tenant_id": t['tenant_id'],
                "tenant_name": t['tenant_name'],
                "phone_number": t['phone_number'],
                "is_active": t['is_active'],
                "display_address": t['display_address'],
                "apartment_name": t['apartment_name'],
                "base_rent_applied": float(t['base_rent_applied'] or 0),
                "security_deposit_applied": float(t['security_deposit_applied'] or 0),
                "collected": float(t['collected']),
                "outstanding": float(t['outstanding'])
            }
            for t in tenants
        ],
        "ledgers": [
            {
                "tenant_id": l['tenant_id'],
                "tenant_name": l['tenant_name'],
                "phone_number": l['phone_number'],
                "display_address": l['display_address'],
                "apartment_name": l['apartment_name'],
                "gross_baki": float(l['gross_baki']),
                "jama": float(l['jama']),
                "new_baki": float(l['new_baki']),
                "payment_status": l['payment_status'],
                "prev_baki": float(l['prev_baki']),
                "month_total": float(l['month_total']),
                "bhadu_applied": float(l['bhadu_applied']),
                "light_bill": float(l['light_bill']),
                "gas_bill": float(l['gas_bill']),
                "maintenance_applied": float(l['maintenance_applied']),
                "common_misc_expenses": float(l['common_misc_expenses'])
            }
            for l in ledgers
        ]
    }


@app.get("/api/dashboard/vacancy")
def get_dashboard_vacancy(upcoming_days: int = Query(120, ge=1)):
    """Detailed vacancy breakdown for the dashboard vacancy card.

    - vacant: rooms with no active tenant, grouped by property with unit list.
    - upcoming: rooms whose current active tenant's lease ends within
      `upcoming_days` days. These are NOT counted as vacant yet.
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT pm.id AS property_id,
               pm.apartment_name,
               r.id AS room_id,
               r.flat_number,
               r.display_address
        FROM rental_core.rooms r
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE NOT EXISTS (
            SELECT 1 FROM rental_core.tenants t
            WHERE t.room_id = r.id AND t.is_active = 'Y'
        )
        ORDER BY pm.apartment_name ASC, r.display_address ASC;
    """)
    vacant_rows = cur.fetchall()

    cur.execute("""
        SELECT pm.id AS property_id,
               pm.apartment_name,
               r.id AS room_id,
               r.flat_number,
               r.display_address,
               t.tenant_name,
               t.phone_number,
               t.lease_end_date
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.is_active = 'Y'
          AND t.lease_end_date IS NOT NULL
          AND t.lease_end_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + %s)
        ORDER BY t.lease_end_date ASC, pm.apartment_name ASC, r.display_address ASC;
    """, (upcoming_days,))
    upcoming_rows = cur.fetchall()

    cur.close()
    conn.close()

    vacant_by_prop = {}
    for r in vacant_rows:
        key = r['property_id']
        entry = vacant_by_prop.setdefault(key, {
            "property_id": key,
            "apartment_name": r['apartment_name'],
            "vacant_units": 0,
            "units": []
        })
        entry["vacant_units"] += 1
        entry["units"].append({
            "room_id": r['room_id'],
            "flat_number": r['flat_number'],
            "display_address": r['display_address']
        })

    upcoming_by_prop = {}
    for r in upcoming_rows:
        key = r['property_id']
        entry = upcoming_by_prop.setdefault(key, {
            "property_id": key,
            "apartment_name": r['apartment_name'],
            "upcoming_units": 0,
            "units": []
        })
        entry["upcoming_units"] += 1
        entry["units"].append({
            "room_id": r['room_id'],
            "flat_number": r['flat_number'],
            "display_address": r['display_address'],
            "tenant_name": r['tenant_name'],
            "phone_number": r['phone_number'],
            "lease_end_date": str(r['lease_end_date']) if r['lease_end_date'] else None
        })

    return {
        "upcoming_days": upcoming_days,
        "vacant_properties": list(vacant_by_prop.values()),
        "vacant_count": sum(v["vacant_units"] for v in vacant_by_prop.values()),
        "upcoming_properties": list(upcoming_by_prop.values()),
        "upcoming_count": sum(u["upcoming_units"] for u in upcoming_by_prop.values())
    }


@app.get("/api/rooms/with-vacancy")
def list_rooms_with_vacancy():
    """Returns rooms grouped by property with vacancy status for smart tenant assignment."""
    conn = get_db()
    cur = conn.cursor()
    
    # Get all rooms with property info and active tenant status
    cur.execute("""
        SELECT r.*, pm.apartment_name, pm.plot_number,
               pm.default_base_rent, pm.default_maintenance_fee, 
               pm.default_security_deposit,
               t.tenant_name, t.is_active, t.lease_end_date
        FROM rental_core.rooms r
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        LEFT JOIN rental_core.tenants t ON t.room_id = r.id AND t.is_active = 'Y'
        ORDER BY pm.apartment_name ASC, r.display_address ASC;
    """)
    rooms = cur.fetchall()
    cur.close()
    conn.close()

    # Group by property and determine vacancy status
    by_property = {}
    for room in rooms:
        prop_key = room['master_id']
        if prop_key not in by_property:
            by_property[prop_key] = {
                "property_id": prop_key,
                "apartment_name": room['apartment_name'],
                "plot_number": room['plot_number'],
                "default_base_rent": room['default_base_rent'],
                "default_maintenance_fee": room['default_maintenance_fee'],
                "default_security_deposit": room['default_security_deposit'],
                "rooms": []
            }
        
        # Determine vacancy status
        if room['tenant_name'] and room['is_active'] == 'Y':
            if room['lease_end_date']:
                status = "upcoming"
                status_label = f"Available {room['lease_end_date']}"
            else:
                status = "occupied"
                status_label = "Occupied"
        else:
            status = "vacant"
            status_label = "Vacant"
        
        by_property[prop_key]["rooms"].append({
            "id": room['id'],
            "flat_number": room['flat_number'],
            "display_address": room['display_address'],
            "status": status,
            "status_label": status_label,
            "current_tenant": room['tenant_name'] if room['tenant_name'] else None,
            "lease_end_date": str(room['lease_end_date']) if room['lease_end_date'] else None
        })
    
    # Convert to list, sort: vacant first, then upcoming, then occupied
    result = list(by_property.values())
    for prop in result:
        prop["rooms"].sort(key=lambda r: {"vacant": 0, "upcoming": 1, "occupied": 2}[r["status"]])
    
    return result


@app.get("/api/properties")
def list_properties():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT pm.*,
               (SELECT COUNT(*) FROM rental_core.rooms r WHERE r.master_id = pm.id) AS room_count,
               (SELECT COUNT(*) FROM rental_core.tenants t
                JOIN rental_core.rooms r ON t.room_id = r.id
                WHERE r.master_id = pm.id AND t.is_active = 'Y') AS occupied_rooms
        FROM rental_core.property_masters pm
        ORDER BY pm.id DESC;
    """)
    props = cur.fetchall()
    cur.close()
    conn.close()
    return props

@app.post("/api/properties")
def create_property(prop: PropertyCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.property_masters 
            (apartment_name, plot_number, default_base_rent, default_maintenance_fee, default_security_deposit, default_common_utility)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
        """, (
            prop.apartment_name, prop.plot_number, prop.default_base_rent, 
            prop.default_maintenance_fee, prop.default_security_deposit, prop.default_common_utility
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        return {"status": "SUCCESS", "property_id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.put("/api/properties/{property_id}")
def update_property(property_id: int, data: PropertyUpdate):
    conn = get_db()
    cur = conn.cursor()
    try:
        fields, params = [], []
        for col in ["apartment_name", "plot_number", "default_base_rent",
                    "default_maintenance_fee", "default_security_deposit", "default_common_utility"]:
            val = getattr(data, col, None)
            if val is not None:
                fields.append(f"{col} = %s")
                params.append(val)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        params.append(property_id)
        cur.execute(f"UPDATE rental_core.property_masters SET {', '.join(fields)} WHERE id = %s;", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Property not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Property #{property_id} updated."}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass

@app.get("/api/rooms")
def list_rooms():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, pm.apartment_name, pm.plot_number,
               (SELECT t.tenant_name FROM rental_core.tenants t
                WHERE t.room_id = r.id AND t.is_active = 'Y' LIMIT 1) AS tenant_name
        FROM rental_core.rooms r
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        ORDER BY r.id DESC;
    """)
    rooms = cur.fetchall()
    cur.close()
    conn.close()
    return rooms

@app.post("/api/rooms")
def create_room(room: RoomCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.rooms (master_id, flat_number, display_address)
            VALUES (%s, %s, %s) RETURNING id;
        """, (room.master_id, room.flat_number, room.display_address))
        new_id = cur.fetchone()['id']
        conn.commit()
        return {"status": "SUCCESS", "room_id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


class RoomUpdate(BaseModel):
    master_id: Optional[int] = None
    flat_number: Optional[str] = None
    display_address: Optional[str] = None


@app.put("/api/rooms/{room_id}")
def update_room(room_id: int, data: RoomUpdate):
    conn = get_db()
    cur = conn.cursor()
    try:
        fields, params = [], []
        if data.master_id is not None:
            fields.append("master_id = %s")
            params.append(data.master_id)
        if data.flat_number is not None:
            fields.append("flat_number = %s")
            params.append(data.flat_number)
        if data.display_address is not None:
            fields.append("display_address = %s")
            params.append(data.display_address)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        params.append(room_id)
        cur.execute(f"UPDATE rental_core.rooms SET {', '.join(fields)} WHERE id = %s;", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Room not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Room #{room_id} updated."}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


# --- 3. TENANT MANAGEMENT ---
@app.get("/api/tenants")
def list_tenants():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            t.id, 
            t.room_id,
            t.tenant_name, 
            t.phone_number, 
            t.is_active, 
            t.base_rent_applied, 
            t.maintenance_applied, 
            t.security_deposit_applied,
            t.lease_start_date,
            t.lease_end_date,
            t.booking_amount_paid,
            t.pending_deposit_due_date,
            r.display_address,
            COALESCE((
                SELECT new_baki FROM rental_core.monthly_ledgers ml
                WHERE ml.tenant_id = t.id
                ORDER BY ml.billing_month DESC LIMIT 1
            ), 0.00) AS outstanding
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        ORDER BY t.id DESC;
    """)
    tenants = cur.fetchall()
    cur.close()
    conn.close()
    return tenants

@app.get("/api/tenants/{tenant_id}/outstanding")
def tenant_outstanding(tenant_id: int, billing_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(new_baki, 0.00) AS outstanding
        FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month = %s;
    """, (tenant_id, billing_month))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        return {"tenant_id": tenant_id, "billing_month": billing_month, "outstanding": 0.0}
    return {"tenant_id": tenant_id, "billing_month": billing_month, "outstanding": float(row['outstanding'])}


@app.get("/api/tenants/{tenant_id}/year_ledger")
def tenant_year_ledger(tenant_id: int, year: int = Query(..., ge=2000, le=2100)):
    """Full customer view: tenant details + all 12 months for a year, with
    payment transactions and misc-expense breakdown per month."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.*, r.display_address, r.master_id,
               pm.apartment_name, pm.plot_number
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        LEFT JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.id = %s
    """, (tenant_id,))
    tenant = cur.fetchone()
    if tenant is None:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Tenant not found")

    cur.execute("""
        SELECT COUNT(*) AS c FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE r.master_id = %s AND t.is_active = 'Y'
    """, (tenant['master_id'],))
    shared_count = cur.fetchone()['c'] or 1

    months = []
    for m in range(1, 13):
        bm = f"{year}-{m:02d}"
        cur.execute("""
            SELECT * FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
        """, (tenant_id, bm))
        lrow = cur.fetchone()

        cur.execute("""
            SELECT id, payment_date, amount, payment_mode, payment_type, note, batch_id
            FROM rental_core.tenant_payments
            WHERE tenant_id = %s AND billing_month = %s
            ORDER BY payment_date, id
        """, (tenant_id, bm))
        pay_rows = cur.fetchall()
        # Group payment days by their actual payment month/year, e.g. "Aug-26(04,11)".
        _day_groups = []
        for p in pay_rows:
            key = p['payment_date'].strftime('%b-%y')
            if _day_groups and _day_groups[-1][0] == key:
                _day_groups[-1][1].add(p['payment_date'].strftime('%d'))
            else:
                _day_groups.append([key, {p['payment_date'].strftime('%d')}])
        payment_dates = [f"{g[0]}({','.join(sorted(g[1]))})" for g in _day_groups]

        misc_expenses = []
        cur.execute("""
            SELECT tbm.service_number, tbm.amount, tbm.bill_type,
                   tbm.last_pay_date, tbm.last_bill_date,
                   tbm.last_bill_amt, tbm.last_billed_units
            FROM rental_core.torrent_bill_master tbm
            JOIN rental_core.utility_metrics um ON tbm.service_number = um.service_number
            WHERE um.room_id = %s AND tbm.execution_month = %s
            ORDER BY tbm.service_number
        """, (tenant['room_id'], bm))
        utility_bills = [
            {
                "service_number": u['service_number'],
                "amount": float(u['amount'] or 0),
                "bill_type": u['bill_type'],
                "last_pay_date": str(u['last_pay_date']) if u['last_pay_date'] else None,
                "last_bill_date": str(u['last_bill_date']) if u['last_bill_date'] else None,
                "last_bill_amt": float(u['last_bill_amt']) if u['last_bill_amt'] is not None else None,
                "last_billed_units": float(u['last_billed_units']) if u['last_billed_units'] is not None else None,
            } for u in cur.fetchall()
        ]

        # Get Gujarat Gas bills for this month (only columns that exist in this DB:
        # customer_number, amount, bill_type, last_pay_date, execution_month)
        cur.execute("""
            SELECT gbm.customer_number AS service_number, gbm.amount, gbm.bill_type,
                   gbm.last_pay_date, gbm.execution_month AS billing_period
            FROM rental_core.gas_bill_master gbm
            JOIN rental_core.gas_metrics gm ON gbm.customer_number = gm.customer_number
            WHERE gm.room_id = %s AND gbm.execution_month = %s
            ORDER BY gbm.customer_number
        """, (tenant['room_id'], bm))
        gas_bills = [
            {
                "service_number": g['service_number'],
                "amount": float(g['amount'] or 0),
                "bill_type": g['bill_type'],
                "last_pay_date": str(g['last_pay_date']) if g['last_pay_date'] else None,
                "billing_period": g['billing_period'],
                "previous_paid_amount": None,  # not stored in this DB
                "consumed_units": None,  # not stored in this DB
            } for g in cur.fetchall()
        ]

        cur.execute("""
            SELECT expense_name, amount, charge_type, room_id
            FROM rental_core.property_maintenance_expenses
            WHERE master_id = %s AND execution_month = %s AND charge_type = 'SHARED'
            ORDER BY id
        """, (tenant['master_id'], bm))
        for e in cur.fetchall():
            misc_expenses.append({
                "expense_name": e['expense_name'],
                "amount": float(e['amount']),
                "charge_type": "SHARED",
                "tenant_share": round(float(e['amount']) / shared_count, 2),
                "shared_between": shared_count,
            })
        cur.execute("""
            SELECT expense_name, amount, charge_type, room_id
            FROM rental_core.property_maintenance_expenses
            WHERE room_id = %s AND execution_month = %s AND charge_type = 'UNIT'
            ORDER BY id
        """, (tenant['room_id'], bm))
        for e in cur.fetchall():
            misc_expenses.append({
                "expense_name": e['expense_name'],
                "amount": float(e['amount']),
                "charge_type": "UNIT",
                "tenant_share": float(e['amount']),
                "shared_between": 1,
            })

        base = {
            "billing_month": bm,
            "payment_dates": payment_dates,
            "payments": [
                {
                    "id": p['id'],
                    "payment_date": str(p['payment_date']),
                    "amount": float(p['amount']),
                    "payment_mode": p['payment_mode'],
                    "payment_type": p['payment_type'],
                    "note": p['note'],
                    "batch_id": p['batch_id'],
                } for p in pay_rows
            ],
            "misc_expenses": misc_expenses,
            "utility_bills": utility_bills,
            "gas_bills": gas_bills,
        }
        def _fnum(v):
            return float(v) if v is not None else None

        if lrow:
            base.update({
                "prev_baki": float(lrow['prev_baki'] or 0),
                "prev_jama": float(lrow['prev_jama'] or 0),
                "bhadu_applied": float(lrow['bhadu_applied'] or 0),
                "light_bill": float(lrow['light_bill'] or 0),
                "gas_bill": float(lrow['gas_bill'] or 0),
                "maintenance_applied": float(lrow['maintenance_applied'] or 0),
                "common_misc_expenses": float(lrow['common_misc_expenses'] or 0),
                "month_total": float(lrow['month_total'] or 0),
                "gross_baki": float(lrow['gross_baki'] or 0),
                "jama": float(lrow['jama'] or 0),
                "new_baki": float(lrow['new_baki'] or 0),
                "payment_status": lrow['payment_status'],
                "jama_date": lrow['jama_date'],
                "light_bill_type": lrow['light_bill_type'],
                # Stored manual overrides (Ledger Edit) + actuals + flag
                "light_bill_actual": _fnum(lrow.get('light_bill_actual')),
                "light_bill_manual": _fnum(lrow.get('light_bill_manual')),
                "gas_bill_actual": _fnum(lrow.get('gas_bill_actual')),
                "gas_bill_manual": _fnum(lrow.get('gas_bill_manual')),
                "maintenance_actual": _fnum(lrow.get('maintenance_actual')),
                "maintenance_manual": _fnum(lrow.get('maintenance_manual')),
                "common_misc_actual": _fnum(lrow.get('common_misc_actual')),
                "common_misc_manual": _fnum(lrow.get('common_misc_manual')),
                "manual_override": bool(lrow.get('manual_override')),
            })
        else:
            base.update({
                "prev_baki": 0.0, "prev_jama": 0.0, "bhadu_applied": 0.0,
                "light_bill": 0.0, "gas_bill": 0.0, "maintenance_applied": 0.0,
                "common_misc_expenses": 0.0, "month_total": 0.0, "gross_baki": 0.0,
                "jama": 0.0, "new_baki": 0.0, "payment_status": "Not Billed",
                "jama_date": '0', "light_bill_type": 'NORMAL',
                "light_bill_actual": None, "light_bill_manual": None,
                "gas_bill_actual": None, "gas_bill_manual": None,
                "maintenance_actual": None, "maintenance_manual": None,
                "common_misc_actual": None, "common_misc_manual": None,
                "manual_override": False,
            })
        months.append(base)

    # Get deposit status for this tenant
    cur.execute("""
        SELECT t.security_deposit_applied AS required,
               COALESCE(SUM(p.amount), 0) AS received
        FROM rental_core.tenants t
        LEFT JOIN rental_core.tenant_payments p 
            ON p.tenant_id = t.id AND p.payment_type = 'DEPOSIT'
        WHERE t.id = %s
        GROUP BY t.id
    """, (tenant_id,))
    deposit_row = cur.fetchone()
    deposit = None
    if deposit_row:
        required = float(deposit_row['required'] or 0)
        received = float(deposit_row['received'] or 0)
        deposit = {
            "required": required,
            "received": received,
            "pending": required - received,
            "status": "Full" if received >= required else ("Partial" if received > 0 else "Zero")
        }

    cur.close()
    conn.close()
    return {"tenant": tenant, "months": months, "deposit": deposit}


# ============================================================================
# 3b. TENANT SETTLEMENT (Final Settlement) ---
# ============================================================================

class FinalSettlementPreview(BaseModel):
    lease_end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    custom_bhadu: Optional[float] = Field(default=None, ge=0)
    custom_light: Optional[float] = Field(default=None, ge=0)
    custom_gas: Optional[float] = Field(default=None, ge=0)
    custom_maintenance: Optional[float] = Field(default=None, ge=0)
    custom_common: Optional[float] = Field(default=None, ge=0)


class SettlementPreview(BaseModel):
    tenant_id: int
    tenant_name: str
    display_address: str
    phone_number: str
    lease_end_date: str
    billing_month: str
    billing_month_label: str
    is_half_month: bool
    prev_baki: float
    prev_jama: float
    bhadu_applied: float
    light_bill: float
    gas_bill: float
    estimated_light: Optional[float]
    estimated_gas: Optional[float]
    maintenance_applied: float
    common_misc_expenses: float
    month_total: float
    gross_baki: float
    jama: float
    new_baki: float
    outstanding: float
    deposit_required: float
    deposit_received: float
    deposit_pending: float
    available_for_settlement: float
    is_receivable: bool
    is_payable: bool
    receivable_from_tenant: float
    payable_to_tenant: float
    deposit_used: float
    deposit_refund: float
    is_receivable: bool
    is_payable: bool
    source: str  # 'existing' or 'provisional'
    deposit_settled: bool
    deposit_settled_amount: float
    deposit_settled_date: Optional[str]


def _settlement_half_rent(base, stored_half):
    """Half-month rent: stored value wins, else mapped/derived. Single definition
    shared by settlement preview and record paths (duplicates drifted before)."""
    if stored_half is not None and stored_half > 0:
        return float(stored_half)
    try:
        br = int(float(base))
        _map = {3300: 1700, 6500: 3400, 7000: 3700, 5000: 2600, 2500: 1350, 3000: 1600, 6400: 3300, 0: 0}
        if br in _map:
            return float(_map[br])
    except:
        pass
    import math
    try:
        return float(math.ceil(float(base or 0) / 2 / 100) * 100)
    except:
        return float(base or 0) / 2


def _settlement_is_half_month(ls, le, bm):
    """True when lease starts on/after the 16th or ends on/before the 15th of bm."""
    try:
        y, m = map(int, bm.split('-'))
        if ls:
            if isinstance(ls, str):
                ls = datetime.strptime(ls[:10], "%Y-%m-%d").date()
            if ls.year == y and ls.month == m and ls.day >= 16:
                return True
        if le:
            if isinstance(le, str):
                le = datetime.strptime(le[:10], "%Y-%m-%d").date()
            if le.year == y and le.month == m and 1 <= le.day <= 15:
                return True
    except:
        return False
    return False


def _compute_settlement(cur, tenant_id, le_date, billing_month, customs):
    """Single source of truth for settlement math, shared by the preview (GET)
    and record (POST) endpoints so the two can never disagree.

    customs: dict with keys bhadu/light/gas/maintenance/common, each a float
    or None. Non-None values override (preview-only here — POST persists them
    to the ledger first, then calls with all-None customs).
    Returns a dict with every intermediate value. Raises 404 if tenant missing.
    """
    # 1. Tenant info
    cur.execute("""
        SELECT t.*, r.display_address, r.master_id, pm.apartment_name, pm.plot_number,
               r.half_rent AS room_half_rent, pm.default_half_rent, pm.default_base_rent,
               r.flat_number, r.display_address
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        LEFT JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.id = %s
    """, (tenant_id,))
    tenant = cur.fetchone()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Previous month's new_baki carried forward
    _prev_bm = (le_date.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur.execute("SELECT COALESCE(new_baki, 0) AS nb FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_id, _prev_bm))
    prev_row = cur.fetchone()
    prev_nb = float(prev_row['nb']) if prev_row and prev_row['nb'] is not None else 0.0
    if prev_nb >= 0:
        prev_baki, prev_jama = prev_nb, 0.0
    else:
        prev_baki, prev_jama = 0.0, -prev_nb

    # Ledger row for the billing month (None => provisional)
    cur.execute("SELECT * FROM rental_core.monthly_ledgers WHERE tenant_id = %s AND billing_month = %s", (tenant_id, billing_month))
    lrow = cur.fetchone()

    # Estimates for provisional months: max of last up-to-6 billed months.
    estimated_light = None
    estimated_gas = None
    if not lrow:
        cur.execute("""
            SELECT COALESCE(light_bill, 0) AS lb, COALESCE(gas_bill, 0) AS gb
            FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month < %s
            ORDER BY billing_month DESC LIMIT 6
        """, (tenant_id, billing_month))
        _hist = cur.fetchall()
        if _hist:
            estimated_light = max(float(r['lb'] or 0) for r in _hist)
            estimated_gas = max(float(r['gb'] or 0) for r in _hist)
        else:
            estimated_light = 0.0
            estimated_gas = 0.0

    custom_bhadu = customs.get('bhadu')
    custom_light = customs.get('light')
    custom_gas = customs.get('gas')
    custom_maintenance = customs.get('maintenance')
    custom_common = customs.get('common')

    is_half = False
    if custom_bhadu is not None:
        bhadu = float(custom_bhadu)
    else:
        base = float(tenant['base_rent_applied'] or tenant['default_base_rent'] or 3300)
        half_val = tenant.get('half_rent_applied') or tenant.get('room_half_rent') or tenant.get('default_half_rent')
        half_val = float(half_val) if half_val else 0
        is_half = _settlement_is_half_month(tenant.get('lease_start_date'), le_date, billing_month)
        half_computed = _settlement_half_rent(base, half_val)
        bhadu = half_computed if is_half else base

    if custom_maintenance is not None:
        maint = float(custom_maintenance)
    else:
        maint = float(tenant['maintenance_applied'] or 0)

    if custom_common is not None:
        common = float(custom_common)
    else:
        common = 0.0

    if custom_light is not None:
        light = float(custom_light)
    else:
        light = estimated_light if estimated_light is not None else 0.0

    if custom_gas is not None:
        gas = float(custom_gas)
    else:
        gas = estimated_gas if estimated_gas is not None else 0.0

    # Ledger values win when a row exists (manual overrides already folded into
    # the effective columns by the DB generated expressions), then customs.
    if lrow:
        bhadu = float(lrow['bhadu_applied'] or 0)
        light = float(lrow.get('light_bill_manual') if lrow.get('light_bill_manual') is not None else (lrow['light_bill'] or 0))
        gas = float(lrow.get('gas_bill_manual') if lrow.get('gas_bill_manual') is not None else (lrow['gas_bill'] or 0))
        maint = float(lrow.get('maintenance_manual') if lrow.get('maintenance_manual') is not None else (lrow['maintenance_applied'] or 0))
        common = float(lrow.get('common_misc_manual') if lrow.get('common_misc_manual') is not None else (lrow['common_misc_expenses'] or 0))
        if custom_bhadu is not None:
            bhadu = float(custom_bhadu)
        if custom_maintenance is not None:
            maint = float(custom_maintenance)
        if custom_light is not None:
            light = float(custom_light)
        if custom_gas is not None:
            gas = float(custom_gas)

    month_total = bhadu + light + gas + maint + common
    gross_baki = prev_baki + month_total - prev_jama

    cur.execute("""
        SELECT t.security_deposit_applied AS required,
               COALESCE(SUM(p.amount), 0) AS received
        FROM rental_core.tenants t
        LEFT JOIN rental_core.tenant_payments p
            ON p.tenant_id = t.id AND p.payment_type = 'DEPOSIT'
        WHERE t.id = %s
        GROUP BY t.id
    """, (tenant_id,))
    deposit_row = cur.fetchone()
    deposit_required = float(deposit_row['required'] or 0) if deposit_row else 0.0
    deposit_received = float(deposit_row['received'] or 0) if deposit_row else 0.0
    deposit_pending = deposit_required - deposit_received

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS settled
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND payment_type = 'SETTLEMENT'
    """, (tenant_id,))
    settled_row = cur.fetchone()
    total_settled = float(settled_row['settled'] or 0) if settled_row else 0.0

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS refunded
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND payment_type = 'REFUND'
    """, (tenant_id,))
    refunded_row = cur.fetchone()
    total_refunded = float(refunded_row['refunded'] or 0) if refunded_row else 0.0

    # jama = RENT collections for this month (settlement payments never touch it)
    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS jama
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND billing_month = %s AND payment_type = 'RENT'
    """, (tenant_id, billing_month))
    jama_row = cur.fetchone()
    jama = float(jama_row['jama'] or 0) if jama_row else 0.0

    outstanding = gross_baki - jama

    if outstanding > 0:
        deposit_used = min(deposit_received, outstanding)
        receivable_from_tenant = outstanding - deposit_used
        deposit_refund = deposit_received - deposit_used
        payable_to_tenant = deposit_refund
        remaining_receivable = receivable_from_tenant - total_settled
        remaining_payable = deposit_refund - total_refunded
        is_receivable = remaining_receivable > 0.01
        is_payable = remaining_payable > 0.01
    else:
        is_receivable = False
        receivable_from_tenant = 0.0
        payable_to_tenant = deposit_received - outstanding  # deposit + credit
        deposit_used = 0.0
        deposit_refund = deposit_received - outstanding
        remaining_receivable = 0.0
        remaining_payable = deposit_refund - total_refunded
        is_payable = remaining_payable > 0.01

    deposit_settled = tenant.get('deposit_settled', False)
    deposit_settled_amount = float(tenant.get('deposit_settled_amount', 0) or 0)
    deposit_settled_date = tenant.get('deposit_settled_date')
    if deposit_settled_date:
        deposit_settled_date = str(deposit_settled_date)

    is_fully_settled = False
    if is_receivable and is_payable:
        is_fully_settled = (remaining_receivable <= 0.01) and (remaining_payable <= 0.01)
    elif is_receivable:
        is_fully_settled = remaining_receivable <= 0.01
    elif is_payable:
        is_fully_settled = remaining_payable <= 0.01
    else:
        is_fully_settled = True

    return {
        "tenant": tenant,
        "lrow": lrow,
        "prev_baki": prev_baki,
        "prev_jama": prev_jama,
        "is_half": is_half,
        "bhadu": bhadu,
        "light": light,
        "gas": gas,
        "maint": maint,
        "common": common,
        "estimated_light": estimated_light,
        "estimated_gas": estimated_gas,
        "month_total": month_total,
        "gross_baki": gross_baki,
        "jama": jama,
        "outstanding": outstanding,
        "deposit_required": deposit_required,
        "deposit_received": deposit_received,
        "deposit_pending": deposit_pending,
        "total_settled": total_settled,
        "total_refunded": total_refunded,
        "is_receivable": is_receivable,
        "is_payable": is_payable,
        "receivable_from_tenant": receivable_from_tenant,
        "payable_to_tenant": payable_to_tenant,
        "deposit_used": deposit_used,
        "deposit_refund": deposit_refund,
        "remaining_receivable": remaining_receivable,
        "remaining_payable": remaining_payable,
        "is_fully_settled": is_fully_settled,
        "source": 'existing' if lrow else 'provisional',
        "deposit_settled": deposit_settled,
        "deposit_settled_amount": deposit_settled_amount,
        "deposit_settled_date": deposit_settled_date,
    }


@app.get("/api/tenants/{tenant_id}/final-settlement")
async def get_final_settlement(
    tenant_id: int,
    lease_end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    custom_bhadu: Optional[float] = Query(None, ge=0),
    custom_light: Optional[float] = Query(None, ge=0),
    custom_gas: Optional[float] = Query(None, ge=0),
    custom_maintenance: Optional[float] = Query(None, ge=0),
    custom_common: Optional[float] = Query(None, ge=0),
):
    """
    Preview the full & final settlement for a tenant at a given lease end date.
    Supports optional custom overrides for bhadu, light, gas, maintenance, common.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        try:
            le_date = datetime.strptime(lease_end_date[:10], "%Y-%m-%d").date()
        except:
            raise HTTPException(status_code=400, detail="Invalid lease_end_date format, use YYYY-MM-DD")
        billing_month = le_date.strftime("%Y-%m")

        # Single source of truth for all settlement math (shared with POST record).
        s = _compute_settlement(cur, tenant_id, le_date, billing_month, {
            'bhadu': custom_bhadu, 'light': custom_light, 'gas': custom_gas,
            'maintenance': custom_maintenance, 'common': custom_common})
        tenant = s['tenant']
        lrow = s['lrow']
        prev_baki = s['prev_baki']
        prev_jama = s['prev_jama']
        is_half = s['is_half']
        bhadu = s['bhadu']
        light = s['light']
        gas = s['gas']
        maint = s['maint']
        common = s['common']
        estimated_light = s['estimated_light']
        estimated_gas = s['estimated_gas']
        month_total = s['month_total']
        gross_baki = s['gross_baki']
        jama = s['jama']
        outstanding = s['outstanding']
        deposit_required = s['deposit_required']
        deposit_received = s['deposit_received']
        deposit_pending = s['deposit_pending']
        total_settled = s['total_settled']
        total_refunded = s['total_refunded']
        is_receivable = s['is_receivable']
        is_payable = s['is_payable']
        receivable_from_tenant = s['receivable_from_tenant']
        payable_to_tenant = s['payable_to_tenant']
        deposit_used = s['deposit_used']
        deposit_refund = s['deposit_refund']
        remaining_receivable = s['remaining_receivable']
        remaining_payable = s['remaining_payable']
        is_fully_settled = s['is_fully_settled']
        source = s['source']
        deposit_settled = s['deposit_settled']
        deposit_settled_amount = s['deposit_settled_amount']
        deposit_settled_date = s['deposit_settled_date']

        # All settlement math comes from _compute_settlement() above (unpacked locals) —
        # the history/whatsapp/return blocks below consume those values directly.

        # Fetch settlement history as real payment rows (so the UI can delete them)
        cur.execute("""
            SELECT id, payment_type, billing_month, payment_date, amount, payment_mode
            FROM rental_core.tenant_payments
            WHERE tenant_id = %s AND payment_type IN ('SETTLEMENT', 'REFUND')
            ORDER BY payment_date, id
        """, (tenant_id,))
        history = [
            {
                "id": h['id'],
                "payment_type": h['payment_type'],
                "billing_month": h['billing_month'],
                "payment_date": str(h['payment_date']),
                "amount": float(h['amount']),
                "payment_mode": h['payment_mode'],
            } for h in cur.fetchall()
        ]

        # WhatsApp preview message + wa.me link for the settlement modal.
        # Reflects the (possibly custom-overridden) preview numbers above.
        def _amt(v):
            v = float(v or 0.0)
            return f"{int(v)}" if v == int(v) else f"{v:.2f}"

        _tname = tenant['tenant_name']
        _addr = tenant['display_address'] or ""
        _header = _tname + (f" ({_addr})" if _addr else "")
        _le_fmt = le_date.strftime("%d-%m-%Y")
        _bm_label = le_date.strftime("%b %Y")
        _wa_lines = [
            f"*Final Settlement — {_header}*",
            f"Lease end: {_le_fmt} | Billing month: {_bm_label}",
            "",
            "Last month dues:",
            f"Rent: ₹{_amt(bhadu)} | Light: ₹{_amt(light)} | Gas: ₹{_amt(gas)} | Maint: ₹{_amt(maint)} | Other: ₹{_amt(common)}",
            f"Month total: ₹{_amt(month_total)} | Prev balance: ₹{_amt(prev_baki)} | Paid: ₹{_amt(jama)}",
            f"Gross due: ₹{_amt(gross_baki)}",
            "",
            f"Security deposit held: ₹{_amt(deposit_received)} (used ₹{_amt(deposit_used)} for dues)",
        ]
        if is_receivable and is_payable:
            _wa_lines.append(f"*Balance payable by you: ₹{_amt(remaining_receivable)}*")
            _wa_lines.append(f"*Refundable to you: ₹{_amt(remaining_payable)}*")
        elif is_receivable:
            _wa_lines.append(f"*Balance payable by you: ₹{_amt(remaining_receivable)}*")
        elif is_payable:
            _wa_lines.append(f"*Refundable to you: ₹{_amt(remaining_payable)}*")
        else:
            _wa_lines.append("No dues — settled. Thank you!")
        _wa_message = "\n".join(_wa_lines)
        try:
            _wa_link = wm.wa_link(tenant.get('phone_number'), _wa_message)
        except Exception:
            _wa_link = None

        # Nested shape required by the settlement modal (tenant/ledger/settlement/deposit/history)
        return {
            "tenant": {
                "id": tenant_id,
                "tenant_name": tenant['tenant_name'],
                "display_address": tenant['display_address'],
                "phone_number": tenant['phone_number'],
                "lease_end_date": lease_end_date,
                "billing_month": billing_month,
            },
            "ledger": {
                "billing_month": billing_month,
                "billing_month_label": le_date.strftime("%b %Y"),
                "is_half_month": is_half if 'is_half' in locals() else False,
                "source": source,
                "prev_baki": prev_baki,
                "prev_jama": prev_jama,
                "bhadu_applied": bhadu,
                "light_bill": light,
                "light_actual": light,
                "gas_bill": gas,
                "estimated_light": estimated_light,
                "estimated_gas": estimated_gas,
                "maintenance_applied": maint,
                "common_misc_expenses": common,
                "month_total": month_total,
                "gross_baki": gross_baki,
                "jama": jama,
                "new_baki": gross_baki - jama,
            },
            "settlement": {
                "outstanding": outstanding,
                "is_receivable": is_receivable,
                "is_payable": is_payable,
                "receivable_from_tenant": receivable_from_tenant,
                "payable_to_tenant": payable_to_tenant,
                "deposit_used": deposit_used,
                "deposit_refund": deposit_refund,
                "remaining_receivable": max(0, remaining_receivable),
                "remaining_payable": max(0, remaining_payable),
                "total_settled": total_settled,
                "total_refunded": total_refunded,
                "is_fully_settled": is_fully_settled,
                "deposit_settled": deposit_settled,
                "deposit_settled_amount": deposit_settled_amount,
                "deposit_settled_date": deposit_settled_date,
            },
            "deposit": {
                "required": deposit_required,
                "received": deposit_received,
                "pending": deposit_pending,
            },
            "history": history,
            "whatsapp_message": _wa_message,
            "whatsapp_link": _wa_link,
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass

class SettlementRecord(BaseModel):
    lease_end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    payment_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    payment_mode: str = Field(default="CASH", pattern="^(CASH|UPI|BANK_TRANSFER|CARD|CHEQUE)$")
    amount: float = Field(..., gt=0)
    payment_type: str = Field(..., pattern="^(SETTLEMENT|REFUND)$")  # SETTLEMENT=tenant pays receivable, REFUND=owner pays refund
    note: Optional[str] = None
    # Optional custom overrides from the Last Month Breakdown inputs. Any value
    # provided here is PERSISTED to the month's ledger (bhadu direct; light/gas/
    # maintenance/common via manual-override columns) so the gross stays stable
    # across reopens. Blank/absent = keep ledger value.
    custom_bhadu: Optional[float] = Field(default=None, ge=0)
    custom_light: Optional[float] = Field(default=None, ge=0)
    custom_gas: Optional[float] = Field(default=None, ge=0)
    custom_maintenance: Optional[float] = Field(default=None, ge=0)
    custom_common: Optional[float] = Field(default=None, ge=0)


@app.post("/api/tenants/{tenant_id}/settlement")
async def record_settlement(tenant_id: int, data: SettlementRecord):
    """
    Record a final settlement payment.
    - For receivable (tenant owes): payment_type = 'SETTLEMENT' (tenant pays)
    - For payable (owner owes): payment_type = 'REFUND' (owner pays tenant)
    Updates deposit settlement tracking and settlement history.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM rental_core.tenants WHERE id = %s", (tenant_id,))
        tenant = cur.fetchone()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        le_date = datetime.strptime(data.lease_end_date[:10], "%Y-%m-%d").date()
        billing_month = le_date.strftime("%Y-%m")
        payment_date = datetime.strptime(data.payment_date[:10], "%Y-%m-%d").date()

        # Previous month's carry (needed by the customs-persist step below;
        # all settlement math itself comes from _compute_settlement).
        _prev_bm = (le_date.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        cur.execute("SELECT COALESCE(new_baki, 0) AS nb FROM rental_core.monthly_ledgers WHERE tenant_id=%s AND billing_month=%s", (tenant_id, _prev_bm))
        prev_row = cur.fetchone()
        prev_nb = float(prev_row['nb']) if prev_row and prev_row['nb'] is not None else 0.0
        if prev_nb >= 0:
            prev_baki, prev_jama = prev_nb, 0.0
        else:
            prev_baki, prev_jama = 0.0, -prev_nb

        # Persist any custom breakdown overrides to the month's ledger FIRST, so the
        # recorded gross is stable: reopening the window later shows identical numbers
        # and the deposit can never be refunded twice off a shifting gross.
        customs = {}
        if data.custom_bhadu is not None:
            customs['bhadu_applied'] = float(data.custom_bhadu)
        if data.custom_light is not None:
            customs['light_bill_manual'] = float(data.custom_light)
        if data.custom_gas is not None:
            customs['gas_bill_manual'] = float(data.custom_gas)
        if data.custom_maintenance is not None:
            customs['maintenance_manual'] = float(data.custom_maintenance)
        if data.custom_common is not None:
            customs['common_misc_manual'] = float(data.custom_common)
        if customs:
            cur.execute("SELECT id FROM rental_core.monthly_ledgers WHERE tenant_id = %s AND billing_month = %s",
                        (tenant_id, billing_month))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO rental_core.monthly_ledgers
                        (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied,
                         light_bill, gas_bill, maintenance_applied, common_misc_expenses)
                    SELECT t.room_id, t.id, %s, %s, %s, 0.00, 0.00, 0.00, 0.00, 0.00
                    FROM rental_core.tenants t WHERE t.id = %s
                    ON CONFLICT (tenant_id, billing_month) DO NOTHING
                """, (billing_month, prev_baki, prev_jama, tenant_id))
            set_clause = ", ".join(f"{k} = %s" for k in customs)
            if any(k.endswith('_manual') for k in customs):
                set_clause += ", manual_override = TRUE"
            cur.execute(f"UPDATE rental_core.monthly_ledgers SET {set_clause} WHERE tenant_id = %s AND billing_month = %s",
                        (*customs.values(), tenant_id, billing_month))
            # Recompute status from stored jama vs new generated month_total
            cur.execute("""
                UPDATE rental_core.monthly_ledgers
                SET payment_status = CASE
                        WHEN jama >= month_total AND month_total > 0 THEN 'Paid'
                        WHEN jama > 0 AND month_total <= 0 THEN 'Paid'
                        WHEN jama > 0 THEN 'Partial'
                        ELSE 'Unpaid'
                    END
                WHERE tenant_id = %s AND billing_month = %s
            """, (tenant_id, billing_month))
            _cascade_ledger_chain(cur, tenant_id, billing_month)
            # Re-read the updated ledger row for settlement math below
            cur.execute("SELECT * FROM rental_core.monthly_ledgers WHERE tenant_id = %s AND billing_month = %s",
                        (tenant_id, billing_month))
            lrow = cur.fetchone()

        # Single source of truth: identical math to the preview (GET), computed AFTER
        # customs were persisted above — validation can never disagree with display.
        s = _compute_settlement(cur, tenant_id, le_date, billing_month,
                                {'bhadu': None, 'light': None, 'gas': None,
                                 'maintenance': None, 'common': None})
        tenant = s['tenant']
        deposit_received = s['deposit_received']

        # Use payment_type from frontend (validated against situation)
        payment_type = data.payment_type

        # Validate payment_type matches the settlement situation
        if payment_type == 'SETTLEMENT':
            # Tenant pays receivable - validate against remaining receivable
            if data.amount > s['remaining_receivable'] + 0.01:
                raise HTTPException(status_code=400, detail=f"Amount exceeds remaining receivable (₹{s['remaining_receivable']:,.2f})")
            is_full_settlement = (data.amount >= s['remaining_receivable'] - 0.01)
        elif payment_type == 'REFUND':
            # Owner pays refund - validate against remaining refundable
            if data.amount > s['remaining_payable'] + 0.01:
                raise HTTPException(status_code=400, detail=f"Amount exceeds remaining payable (₹{s['remaining_payable']:,.2f})")
            is_full_settlement = (data.amount >= s['remaining_payable'] - 0.01)
        else:
            raise HTTPException(status_code=400, detail="Invalid payment_type. Use SETTLEMENT or REFUND")

        # Insert settlement payment
        cur.execute("SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_batch FROM rental_core.tenant_payments")
        batch_id = cur.fetchone()['next_batch']

        cur.execute("""
            INSERT INTO rental_core.tenant_payments
            (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, note, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
        """, (
            tenant_id,
            tenant['room_id'],
            billing_month,
            payment_date,
            data.amount,
            data.payment_mode,
            payment_type,
            data.note or f'Final settlement {data.lease_end_date}',
            batch_id
        ))
        payment_id = cur.fetchone()['id']

        # Update tenant deposit settlement tracking
        settlement_entry = {
            "payment_id": payment_id,
            "lease_end_date": data.lease_end_date,
            "payment_date": data.payment_date,
            "payment_type": payment_type,
            "amount": float(data.amount),
            "payment_mode": data.payment_mode,
            "note": data.note or f'Final settlement {data.lease_end_date}',
            "recorded_at": datetime.now().isoformat()
        }

        cur.execute("SELECT settlement_history FROM rental_core.tenants WHERE id = %s", (tenant_id,))
        hist_row = cur.fetchone()
        settlement_history = hist_row['settlement_history'] if hist_row and hist_row['settlement_history'] else []
        settlement_history.append(settlement_entry)

        # Update deposit_settled flags
        new_deposit_settled = False
        new_deposit_settled_amount = float(tenant.get('deposit_settled_amount', 0) or 0)
        new_deposit_settled_date = tenant.get('deposit_settled_date')

        if payment_type == 'SETTLEMENT':
            new_deposit_settled_amount += data.amount
            if is_full_settlement:
                new_deposit_settled = True
                new_deposit_settled_date = payment_date
        elif payment_type == 'REFUND':
            new_deposit_settled_amount = deposit_received
            if is_full_settlement:
                new_deposit_settled = True
                new_deposit_settled_date = payment_date

        cur.execute("""
            UPDATE rental_core.tenants
            SET settlement_history = %s,
                deposit_settled = %s,
                deposit_settled_amount = %s,
                deposit_settled_date = %s,
                is_active = CASE WHEN %s THEN 'N' ELSE is_active END
            WHERE id = %s
        """, (json.dumps(settlement_history), new_deposit_settled, new_deposit_settled_amount, new_deposit_settled_date, is_full_settlement, tenant_id))

        # NOTE: SETTLEMENT and REFUND are final settlement actions - they do NOT update monthly ledger jama
        # The ledger reflects monthly rent/bills. Settlement is a separate closing action.
        # No ledger sync needed for settlement payments.

        conn.commit()
        return {
            "status": "SUCCESS",
            "payment_id": payment_id,
            "payment_type": payment_type,
            "message": f"Settlement recorded as {payment_type}",
            "is_full_settlement": is_full_settlement
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass

@app.post("/api/tenants")
def create_tenant(tenant: TenantCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        # Empty strings must become NULL for DATE columns
        for _f in ("pending_deposit_due_date", "lease_start_date", "lease_end_date"):
            if getattr(tenant, _f) == "":
                setattr(tenant, _f, None)
        cur.execute("""
            INSERT INTO rental_core.tenants 
            (room_id, tenant_name, phone_number, base_rent_applied, maintenance_applied, security_deposit_applied, 
             booking_amount_paid, pending_deposit_due_date, lease_start_date, lease_end_date, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Y') RETURNING id;
        """, (
            tenant.room_id, tenant.tenant_name, tenant.phone_number, 
            tenant.base_rent_applied, tenant.maintenance_applied, tenant.security_deposit_applied,
            tenant.booking_amount_paid, tenant.pending_deposit_due_date,
            tenant.lease_start_date, tenant.lease_end_date
        ))
        new_id = cur.fetchone()['id']
        
        # If booking amount was paid, create a DEPOSIT payment record
        if tenant.booking_amount_paid and tenant.booking_amount_paid > 0:
            cur.execute("""
                INSERT INTO rental_core.tenant_payments 
                (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, note, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'DEPOSIT', %s, %s);
            """, (
                new_id,
                tenant.room_id,
                tenant.lease_start_date[:7] if tenant.lease_start_date else datetime.now().strftime("%Y-%m"),
                tenant.lease_start_date if tenant.lease_start_date else datetime.now().date(),
                tenant.booking_amount_paid,
                'CASH',
                f'Booking amount for security deposit - {tenant.tenant_name}',
                None  # batch_id
            ))
        
        conn.commit()
        return {"status": "SUCCESS", "tenant_id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


# --- 4. MAINTENANCE EXPENSES ---
@app.get("/api/expenses/months")
def list_expense_months():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT execution_month
        FROM rental_core.property_maintenance_expenses
        ORDER BY execution_month DESC;
    """)
    months = [r['execution_month'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return months

@app.get("/api/expenses/totals")
def expense_monthly_totals():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT execution_month,
               COALESCE(SUM(amount), 0.00) AS total_amount,
               COUNT(*) AS expense_count
        FROM rental_core.property_maintenance_expenses
        GROUP BY execution_month
        ORDER BY execution_month DESC;
    """)
    totals = cur.fetchall()
    cur.close()
    conn.close()
    return totals

@app.get("/api/expenses")
def list_expenses(execution_month: str = None):
    if not execution_month:
        execution_month = current_billing_period()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.*, pm.apartment_name, pm.plot_number, r.display_address AS room_address
        FROM rental_core.property_maintenance_expenses e
        JOIN rental_core.property_masters pm ON e.master_id = pm.id
        LEFT JOIN rental_core.rooms r ON e.room_id = r.id
        WHERE e.execution_month = %s
        ORDER BY e.id DESC;
    """, (execution_month,))
    expenses = cur.fetchall()
    cur.close()
    conn.close()
    return expenses

@app.post("/api/expenses")
def add_expense(expense: MaintenanceExpenseCreate):
    execution_month = current_billing_period()
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.property_maintenance_expenses 
            (master_id, expense_name, amount, execution_month, charge_type, room_id)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
        """, (expense.master_id, expense.expense_name, expense.amount, execution_month,
              expense.charge_type, expense.room_id))
        new_id = cur.fetchone()['id']
        conn.commit()
        return {"status": "SUCCESS", "expense_id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


class ExpenseUpdate(BaseModel):
    master_id: Optional[int] = None
    expense_name: Optional[str] = None
    amount: Optional[float] = Field(default=None, ge=0)
    execution_month: Optional[str] = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    charge_type: Optional[str] = Field(default=None, pattern="^(SHARED|UNIT|OWNER)$")
    room_id: Optional[int] = None


@app.put("/api/expenses/{expense_id}")
def update_expense(expense_id: int, data: ExpenseUpdate):
    conn = get_db()
    cur = conn.cursor()
    try:
        fields, params = [], []
        if data.master_id is not None:
            fields.append("master_id = %s"); params.append(data.master_id)
        if data.expense_name is not None:
            fields.append("expense_name = %s"); params.append(data.expense_name)
        if data.amount is not None:
            fields.append("amount = %s"); params.append(data.amount)
        if data.execution_month is not None:
            fields.append("execution_month = %s"); params.append(data.execution_month)
        if data.charge_type is not None:
            fields.append("charge_type = %s"); params.append(data.charge_type)
        if data.room_id is not None:
            fields.append("room_id = %s"); params.append(data.room_id)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        params.append(expense_id)
        cur.execute(f"UPDATE rental_core.property_maintenance_expenses SET {', '.join(fields)} WHERE id = %s;", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Expense not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Expense #{expense_id} updated."}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM rental_core.property_maintenance_expenses WHERE id = %s;", (expense_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Expense not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Expense #{expense_id} deleted."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


# --- 5. UTILITY SERVICE MAPPINGS ---
@app.get("/api/mappings/electricity")
def get_electric_mappings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT um.id, um.account_id, um.room_id, um.service_number, r.display_address
        FROM rental_core.utility_metrics um
        LEFT JOIN rental_core.rooms r ON um.room_id = r.id;
    """)
    mappings = cur.fetchall()
    cur.close()
    conn.close()
    return mappings

@app.get("/api/mappings/gas")
def get_gas_mappings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT gm.id, gm.room_id, gm.customer_number, r.display_address
        FROM rental_core.gas_metrics gm
        LEFT JOIN rental_core.rooms r ON gm.room_id = r.id;
    """)
    mappings = cur.fetchall()
    cur.close()
    conn.close()
    return mappings


# --- 6. MONTHLY LEDGER VIEW ---
@app.get("/api/ledgers/months")
def list_ledger_months():
    """Distinct billing months available in the ledgers OR payments, newest first."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT billing_month FROM (
            SELECT billing_month FROM rental_core.monthly_ledgers
            UNION
            SELECT billing_month FROM rental_core.tenant_payments
            UNION
            SELECT billing_month FROM rental_core.tenant_reminders
        ) m
        ORDER BY billing_month DESC;
    """)
    months = [r['billing_month'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return months


@app.get("/api/ledgers")
@app.get("/api/ledgers/current")
def get_ledgers(billing_month: str = None):
    if not billing_month:
        billing_month = current_billing_period()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            ml.id,
            ml.room_id,
            ml.tenant_id,
            ml.billing_month,
            ml.prev_baki,
            ml.prev_jama,
            ml.bhadu_applied,
            ml.light_bill,
            ml.gas_bill,
            ml.maintenance_applied,
            ml.common_misc_expenses,
            ml.jama,
            ml.jama_date,
            ml.new_baki,
            ml.payment_status,
            ml.light_bill_type,
            ml.month_total,
            ml.gross_baki,
            r.display_address,
            pm.id AS property_id,
            pm.apartment_name,
            t.tenant_name,
            t.phone_number
        FROM rental_core.monthly_ledgers ml
        LEFT JOIN rental_core.rooms r ON ml.room_id = r.id
        LEFT JOIN rental_core.property_masters pm ON r.master_id = pm.id
        LEFT JOIN rental_core.tenants t ON ml.tenant_id = t.id
        WHERE ml.billing_month = %s
        ORDER BY r.display_address ASC;
    """, (billing_month,))
    ledgers = cur.fetchall()
    cur.close()
    conn.close()
    return ledgers


# --- 6b. TENANT PAYMENTS (rent + deposit) ---

def _split_carry(nb):
    """Split a carried-forward net balance into non-negative (prev_baki,
    prev_jama) parts. Positive carry (debt) -> prev_baki, negative carry
    (overpayment/credit) -> prev_jama."""
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
        # Ensure a ledger row exists so advance payments aren't lost before the
        # month is compiled: carry forward prior month balance as prev_baki
        # (or overpayment as prev_jama credit).
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
                WHEN %s >= month_total AND month_total > 0 THEN 'Paid'
                WHEN %s > 0 AND month_total <= 0 THEN 'Paid'
                WHEN %s > 0 THEN 'Partial'
                ELSE 'Unpaid'
            END
        WHERE tenant_id = %s AND billing_month = %s
    """, (total_paid, row['last_pay_date'], total_paid, total_paid, total_paid,
          tenant_id, billing_month))

    if total_paid <= 0:
        # If no payments remain and the ledger was only a placeholder created by
        # an advance payment (no bills compiled yet), remove it.
        cur.execute("""
            DELETE FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
              AND bhadu_applied = 0 AND light_bill = 0 AND gas_bill = 0
              AND maintenance_applied = 0 AND common_misc_expenses = 0
              AND jama = 0;
        """, (tenant_id, billing_month))

    _cascade_ledger_chain(cur, tenant_id, billing_month)


def _cascade_ledger_chain(cur, tenant_id, from_month):
    """Recompute prev_baki/prev_jama for every month after from_month so a
    payment recorded against an older month flows through the whole ledger
    chain (new_baki is generated, but prev_baki/prev_jama are stored)."""
    cur.execute("""
        SELECT billing_month FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month > %s
        ORDER BY billing_month ASC
    """, (tenant_id, from_month))
    months = [r['billing_month'] for r in cur.fetchall()]
    if not months:
        return

    cur.execute("""
        SELECT COALESCE(new_baki, 0.00) AS new_baki,
               COALESCE(prev_jama, 0.00) AS prev_jama
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


@app.get("/api/payments")
def list_payments(billing_month: str = None):
    if not billing_month:
        billing_month = current_billing_period()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.tenant_id, p.room_id, p.billing_month, p.payment_date, p.amount,
               p.payment_mode, p.payment_type, p.note, p.created_at, p.batch_id,
               t.tenant_name, r.display_address
        FROM rental_core.tenant_payments p
        LEFT JOIN rental_core.tenants t ON p.tenant_id = t.id
        LEFT JOIN rental_core.rooms r ON p.room_id = r.id
        WHERE p.billing_month = %s
        ORDER BY p.payment_date DESC, p.id DESC
    """, (billing_month,))
    payments = cur.fetchall()
    cur.close()
    conn.close()
    return payments


@app.post("/api/payments")
def add_payment(payment: PaymentCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT room_id FROM rental_core.tenants WHERE id = %s", (payment.tenant_id,))
        row = cur.fetchone()
        room_id = row['room_id'] if row else None

        # Build allocation list: explicit split or single month/amount
        if payment.allocations:
            allocs = [(a.billing_month, a.amount) for a in payment.allocations]
        else:
            if not payment.billing_month or payment.amount is None:
                raise HTTPException(status_code=400, detail="Provide billing_month+amount or allocations list")
            allocs = [(payment.billing_month, payment.amount)]

        # Allocate batch id so one payment can fan out across several months
        cur.execute("SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_batch FROM rental_core.tenant_payments")
        batch_id = cur.fetchone()['next_batch']

        new_ids = []
        affected_months = set()
        for bm, amt in allocs:
            cur.execute("""
                INSERT INTO rental_core.tenant_payments
                (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, note, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (payment.tenant_id, room_id, bm, payment.payment_date,
                  amt, payment.payment_mode, payment.payment_type, payment.note, batch_id))
            new_ids.append(cur.fetchone()['id'])
            if payment.payment_type == "RENT":
                affected_months.add(bm)

        # Re-sync ledgers for every month this payment touched
        for bm in affected_months:
            _sync_ledger_jama(cur, payment.tenant_id, bm)
        conn.commit()
        return {"status": "SUCCESS", "payment_ids": new_ids, "batch_id": batch_id}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.delete("/api/payments/{payment_id}")
def delete_payment(payment_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT tenant_id, billing_month, payment_type, batch_id FROM rental_core.tenant_payments WHERE id = %s",
                    (payment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Payment not found")

        # Deleting any row of a split payment deletes the whole batch.
        # NOTE: batch_id can be NULL (old/single payments) — then delete just this row.
        if row['batch_id'] is None:
            batch_rows = [{"tenant_id": row['tenant_id'], "billing_month": row['billing_month']}]
            deleted_ids = [payment_id]
            cur.execute("DELETE FROM rental_core.tenant_payments WHERE id = %s", (payment_id,))
        else:
            cur.execute("SELECT id, tenant_id, billing_month FROM rental_core.tenant_payments WHERE batch_id = %s",
                        (row['batch_id'],))
            batch_rows = cur.fetchall()
            deleted_ids = [b['id'] for b in batch_rows]
            cur.execute("DELETE FROM rental_core.tenant_payments WHERE batch_id = %s", (row['batch_id'],))
        for b in batch_rows:
            _sync_ledger_jama(cur, b['tenant_id'], b['billing_month'])

        # If a SETTLEMENT/REFUND payment was deleted, reopen the settlement bookkeeping:
        # drop its history entries, recompute settled flags, reactivate tenant if fully reopened.
        if row['payment_type'] in ('SETTLEMENT', 'REFUND'):
            tenant_id = row['tenant_id']
            cur.execute("SELECT settlement_history FROM rental_core.tenants WHERE id = %s", (tenant_id,))
            hist_row = cur.fetchone()
            history = hist_row['settlement_history'] if hist_row and hist_row['settlement_history'] else []
            history = [h for h in history if h.get('payment_id') not in deleted_ids]
            cur.execute("""
                SELECT COALESCE(SUM(amount) FILTER (WHERE payment_type = 'SETTLEMENT'), 0) AS settled,
                       COALESCE(SUM(amount) FILTER (WHERE payment_type = 'REFUND'), 0) AS refunded
                FROM rental_core.tenant_payments WHERE tenant_id = %s
            """, (tenant_id,))
            rem = cur.fetchone()
            remaining_settled = float(rem['settled'] or 0)
            remaining_refunded = float(rem['refunded'] or 0)
            fully_reopened = (remaining_settled <= 0.01 and remaining_refunded <= 0.01)
            cur.execute("""
                UPDATE rental_core.tenants
                SET settlement_history = %s,
                    deposit_settled = CASE WHEN %s THEN FALSE ELSE deposit_settled END,
                    deposit_settled_amount = %s,
                    deposit_settled_date = CASE WHEN %s THEN NULL ELSE deposit_settled_date END,
                    is_active = CASE WHEN %s THEN 'Y' ELSE is_active END
                WHERE id = %s
            """, (json.dumps(history), fully_reopened, remaining_settled, fully_reopened, fully_reopened, tenant_id))
        conn.commit()
        return {"status": "SUCCESS", "deleted_payments": len(batch_rows)}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.get("/api/gas-reports/status")
def get_gas_reports_status():
    """Check if gas report files exist and their last modified times."""
    import os
    from datetime import datetime, timedelta
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(base_dir, "gujarat_gas_report.csv")
    xlsx_path = os.path.join(base_dir, "gujarat_gas_report.xlsx")
    
    csv_exists = os.path.exists(csv_path)
    xlsx_exists = os.path.exists(xlsx_path)
    
    last_updated = None
    if csv_exists:
        last_updated = datetime.fromtimestamp(os.path.getmtime(csv_path)).isoformat()
    
    return {
        "csvExists": csv_exists,
        "xlsxExists": xlsx_exists,
        "lastUpdated": last_updated,
        "csvPath": csv_path if csv_exists else None,
        "xlsxPath": xlsx_path if xlsx_exists else None
    }


@app.get("/api/gas-reports/download")
def download_gas_report(type: str = "csv"):
    """Download gas report file (csv or xlsx)."""
    import os
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    if type == "csv":
        file_path = os.path.join(base_dir, "gujarat_gas_report.csv")
        media_type = "text/csv"
        filename = "gujarat_gas_report.csv"
    elif type == "xlsx":
        file_path = os.path.join(base_dir, "gujarat_gas_report.xlsx")
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "gujarat_gas_report.xlsx"
    else:
        raise HTTPException(status_code=400, detail="Invalid type. Use 'csv' or 'xlsx'")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"{type.upper()} report not found. Generate it first.")
    
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename
    )


@app.get("/api/deposits")
def get_deposit_status():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id AS tenant_id, t.tenant_name, t.security_deposit_applied AS required_deposit,
               r.display_address,
               COALESCE((
                   SELECT SUM(p.amount) FROM rental_core.tenant_payments p
                   WHERE p.tenant_id = t.id AND p.payment_type = 'DEPOSIT'
               ), 0.00) AS deposit_received
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE t.is_active = 'Y'
        ORDER BY r.display_address
    """)
    deposits = cur.fetchall()
    cur.close()
    conn.close()
    return deposits


# --- 6c. LEDGER EDITING ---

class LedgerUpdate(BaseModel):
    jama: Optional[float] = Field(default=None, ge=0)
    jama_date: Optional[str] = None
    payment_status: Optional[str] = Field(default=None, pattern="^(Paid|Partial|Unpaid)$")


@app.put("/api/ledgers/{ledger_id}")
def update_ledger(ledger_id: int, data: LedgerUpdate):
    """Edit jama / jama_date / payment_status for a ledger month.

    jama (collected amount) is validated against RENT payments recorded in
    tenant_payments for the same tenant+month, so the two never diverge:
      - if RENT payments exist, jama must equal their total (auto-corrected);
      - if the requested jama differs from recorded payments, we reject it.
    payment_status is recomputed from month_total vs jama unless explicitly supplied.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT tenant_id, billing_month, month_total, jama, jama_date, payment_status
            FROM rental_core.monthly_ledgers WHERE id = %s;
        """, (ledger_id,))
        ledger = cur.fetchone()
        if not ledger:
            raise HTTPException(status_code=404, detail="Ledger entry not found")

        month_total = float(ledger['month_total'] or 0.00)
        tenant_id = ledger['tenant_id']
        billing_month = ledger['billing_month']

        cur.execute("""
            SELECT COALESCE(SUM(amount), 0.00) AS paid
            FROM rental_core.tenant_payments
            WHERE tenant_id = %s AND billing_month = %s AND payment_type = 'RENT';
        """, (tenant_id, billing_month))
        payments_total = float(cur.fetchone()['paid'])

        if data.jama is not None:
            new_jama = float(data.jama)
            if payments_total > 0 and abs(new_jama - payments_total) > 0.005:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot set jama to ₹{new_jama:,.2f}: RENT payments totaling "
                        f"₹{payments_total:,.2f} are recorded for this tenant/month. "
                        "Adjust or delete the payment records instead."
                    )
                )
            if payments_total > 0:
                new_jama = payments_total
        else:
            new_jama = float(ledger['jama'] or 0.00)

        new_jama_date = data.jama_date if data.jama_date is not None else ledger['jama_date']

        if data.payment_status:
            new_status = data.payment_status
        else:
            new_status = "Unpaid"
            if month_total > 0 and new_jama >= month_total:
                new_status = "Paid"
            elif new_jama > 0:
                new_status = "Partial"

        cur.execute("""
            UPDATE rental_core.monthly_ledgers
            SET jama = %s, jama_date = %s, payment_status = %s
            WHERE id = %s;
        """, (new_jama, new_jama_date, new_status, ledger_id))
        conn.commit()
        return {
            "status": "SUCCESS",
            "ledger_id": ledger_id,
            "jama": new_jama,
            "jama_date": new_jama_date,
            "payment_status": new_status,
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


class LedgerManualUpdate(BaseModel):
    light_bill: Optional[float] = Field(default=None, ge=0)
    gas_bill: Optional[float] = Field(default=None, ge=0)
    maintenance: Optional[float] = Field(default=None, ge=0)
    common_misc: Optional[float] = Field(default=None, ge=0)
    clear: Optional[bool] = False


@app.put("/api/ledgers/{ledger_id}/manual")
def update_ledger_manual(ledger_id: int, data: LedgerManualUpdate):
    """Save or clear manual bill overrides for a ledger month.

    Set payload: {light_bill?, gas_bill?, maintenance?, common_misc?} — only the
    provided fields are overwritten; manual_override is set TRUE.
    Clear payload: {clear: true} — clears the provided fields, or ALL manuals
    when no field keys are given; manual_override recomputed from remainder.
    payment_status is recomputed and the ledger chain cascaded, since generated
    month_total/gross_baki/new_baki shift with the overrides.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT tenant_id, billing_month FROM rental_core.monthly_ledgers WHERE id = %s",
                    (ledger_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        tenant_id, billing_month = row['tenant_id'], row['billing_month']

        col_map = {
            'light_bill': 'light_bill_manual',
            'gas_bill': 'gas_bill_manual',
            'maintenance': 'maintenance_manual',
            'common_misc': 'common_misc_manual',
        }
        provided = {k for k in col_map if getattr(data, k) is not None}
        if data.clear:
            to_clear = provided if provided else set(col_map)
            set_clause = ", ".join(f"{col_map[k]} = NULL" for k in to_clear)
            cur.execute(f"UPDATE rental_core.monthly_ledgers SET {set_clause} WHERE id = %s",
                        (ledger_id,))
        else:
            if not provided:
                raise HTTPException(status_code=400, detail="No manual values to save")
            set_clause = ", ".join(f"{col_map[k]} = %s" for k in provided)
            cur.execute(f"UPDATE rental_core.monthly_ledgers SET {set_clause} WHERE id = %s",
                        (*[float(getattr(data, k)) for k in provided], ledger_id))

        cur.execute("""
            UPDATE rental_core.monthly_ledgers
            SET manual_override = (
                    light_bill_manual IS NOT NULL OR gas_bill_manual IS NOT NULL
                    OR maintenance_manual IS NOT NULL OR common_misc_manual IS NOT NULL
                ),
                payment_status = CASE
                    WHEN jama >= month_total AND month_total > 0 THEN 'Paid'
                    WHEN jama > 0 AND month_total <= 0 THEN 'Paid'
                    WHEN jama > 0 THEN 'Partial'
                    ELSE 'Unpaid'
                END
            WHERE id = %s
            RETURNING manual_override, payment_status, month_total, gross_baki, new_baki;
        """, (ledger_id,))
        updated = cur.fetchone()
        _cascade_ledger_chain(cur, tenant_id, billing_month)
        conn.commit()
        return {
            "status": "SUCCESS",
            "ledger_id": ledger_id,
            "manual_override": bool(updated['manual_override']),
            "payment_status": updated['payment_status'],
            "month_total": float(updated['month_total'] or 0),
            "gross_baki": float(updated['gross_baki'] or 0),
            "new_baki": float(updated['new_baki'] or 0),
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


# --- 7. ONE-CLICK OPERATIONAL ACTIONS ---
@app.post("/api/operations/run-torrent-scraping")
def trigger_torrent_scraping(background_tasks: BackgroundTasks):
    def run_scraper():
        subprocess.run([sys.executable, "torrent_automation_engine.py"], check=True)

    background_tasks.add_task(run_scraper)
    return {"status": "QUEUED", "message": "Torrent Power automation scraper triggered in background."}

@app.post("/api/operations/allocate-bills")
def trigger_bill_allocation():
    try:
        result = subprocess.run(
            [sys.executable, "allocate_tenant_bills.py"],
            capture_output=True, text=True, check=False
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Allocation failed: {stderr or stdout}")
        # Parse the report line emitted by the script: ALLOC_REPORT JSON
        report = None
        for line in stdout.splitlines():
            if line.startswith("ALLOC_REPORT "):
                try:
                    report = json.loads(line[len("ALLOC_REPORT "):])
                except Exception:
                    report = None
                break
        return {
            "status": "SUCCESS",
            "message": "Bills allocated and ledgers compiled successfully.",
            "report": report or {"total": 0, "success": 0, "failures": []},
            "raw_output": stdout,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 8. STATIC FILES & SERVE FRONTEND ---
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=FileResponse)
def serve_gui():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    raise HTTPException(status_code=404, detail="Frontend static/index.html not found.")

@app.get("/index_v1", response_class=FileResponse)
def serve_gui_v1():
    """Legacy GUI backup (pre-responsive). Also reachable at /static/index_v1.html."""
    if os.path.exists("static/index_v1.html"):
        return FileResponse("static/index_v1.html")
    raise HTTPException(status_code=404, detail="Legacy GUI static/index_v1.html not found.")


@app.get("/sw.js", include_in_schema=False)
def serve_service_worker():
    """PWA service worker served from root so its scope covers the whole app."""
    if os.path.exists("static/sw.js"):
        return FileResponse("static/sw.js", media_type="application/javascript", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        })
    raise HTTPException(status_code=404, detail="Service worker not found.")



class TenantUpdate(BaseModel):
    tenant_name: Optional[str] = None
    room_id: Optional[int] = None
    phone_number: Optional[str] = None
    base_rent_applied: Optional[float] = Field(default=None, ge=0)
    maintenance_applied: Optional[float] = Field(default=None, ge=0)
    security_deposit_applied: Optional[float] = Field(default=None, ge=0)
    booking_amount_paid: Optional[float] = Field(default=None, ge=0)
    pending_deposit_due_date: Optional[str] = None
    is_active: Optional[str] = Field(default=None, pattern="^[YN]$")
    lease_start_date: Optional[str] = None
    lease_end_date: Optional[str] = None

@app.put("/api/tenants/{tenant_id}")
def update_tenant(tenant_id: int, data: TenantUpdate):
    """Updates tenant details, rent terms, lease dates or active flag."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM rental_core.tenants WHERE id = %s;", (tenant_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Tenant not found")

        updates = {}
        for col in ["tenant_name", "room_id", "phone_number", "is_active",
                    "lease_start_date", "lease_end_date", "booking_amount_paid", "pending_deposit_due_date"]:
            val = getattr(data, col, None)
            if val is not None:
                updates[col] = val
        for col in ["base_rent_applied", "maintenance_applied", "security_deposit_applied"]:
            val = getattr(data, col, None)
            if val is not None:
                updates[col] = val

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        if updates.get('lease_start_date') == '':
            updates['lease_start_date'] = None
        if updates.get('lease_end_date') == '':
            updates['lease_end_date'] = None
        if updates.get('pending_deposit_due_date') == '':
            updates['pending_deposit_due_date'] = None
        if updates.get('room_id'):
            cur.execute("SELECT COUNT(*) AS cnt FROM rental_core.rooms WHERE id = %s", (updates['room_id'],))
            if cur.fetchone()['cnt'] == 0:
                raise HTTPException(status_code=400, detail=f"Room #{updates['room_id']} does not exist")

        set_clause = ", ".join(f"{c} = %s" for c in updates)
        params = list(updates.values()) + [tenant_id]
        cur.execute(f"UPDATE rental_core.tenants SET {set_clause} WHERE id = %s;", params)
        conn.commit()
        return {"status": "SUCCESS", "message": f"Tenant #{tenant_id} updated."}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass

@app.post("/api/operations/import-gas-metrics")
def trigger_gas_import():
    """Triggers the Gujarat Gas CSV parser script."""
    try:
        subprocess.run([sys.executable, "import_gas_metrics.py"], check=True)
        return {"status": "SUCCESS", "message": "Piped gas metrics imported from CSV successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 9. WHATSAPP MESSAGE TEMPLATES ---

class TemplateCreate(BaseModel):
    template_name: str
    template_type: Optional[str] = Field(default="REMINDER")
    body: str
    note: Optional[str] = ""
    signature: Optional[str] = ""
    is_active: Optional[str] = Field(default="Y", pattern="^[YN]$")
    is_default: Optional[str] = Field(default="N", pattern="^[YN]$")


class TemplateUpdate(BaseModel):
    template_name: Optional[str] = None
    template_type: Optional[str] = None
    body: Optional[str] = None
    note: Optional[str] = None
    signature: Optional[str] = None
    is_active: Optional[str] = Field(default=None, pattern="^[YN]$")
    is_default: Optional[str] = Field(default=None, pattern="^[YN]$")


@app.get("/api/templates")
def list_templates():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.*, COUNT(m.tenant_id) AS mapped_tenants
        FROM rental_core.message_templates t
        LEFT JOIN rental_core.tenant_message_templates m ON t.id = m.template_id
        GROUP BY t.id
        ORDER BY t.is_default DESC, t.id DESC;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.post("/api/templates")
def create_template(tpl: TemplateCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.message_templates
            (template_name, template_type, body, note, signature, is_active, is_default)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;
        """, (tpl.template_name, tpl.template_type, tpl.body,
              tpl.note or "", tpl.signature or "", tpl.is_active, tpl.is_default))
        new_id = cur.fetchone()['id']
        conn.commit()
        return {"status": "SUCCESS", "template_id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.put("/api/templates/{template_id}")
def update_template(template_id: int, data: TemplateUpdate):
    conn = get_db()
    cur = conn.cursor()
    try:
        fields, params = [], []
        for col in ["template_name", "template_type", "body", "note", "signature",
                    "is_active", "is_default"]:
            val = getattr(data, col, None)
            if val is not None:
                fields.append(f"{col} = %s")
                params.append(val)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        params.append(template_id)
        cur.execute(f"UPDATE rental_core.message_templates SET {', '.join(fields)} WHERE id = %s;", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Template not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Template #{template_id} updated."}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM rental_core.message_templates WHERE id = %s;", (template_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Template not found")
        conn.commit()
        return {"status": "SUCCESS", "message": f"Template #{template_id} deleted."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


# --- 9b. PER-TENANT TEMPLATE MAPPING ---

@app.get("/api/templates/mappings")
def list_template_mappings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.tenant_id, m.template_id, m.created_at,
               t.tenant_name, r.display_address, tpl.template_name
        FROM rental_core.tenant_message_templates m
        JOIN rental_core.tenants t ON m.tenant_id = t.id
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        LEFT JOIN rental_core.message_templates tpl ON m.template_id = tpl.id
        ORDER BY r.display_address;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.put("/api/templates/mappings/{tenant_id}")
def set_tenant_template(tenant_id: int, template_id: int = Query(None)):
    """Map a template to a tenant (template_id=0 or missing clears mapping)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM rental_core.tenants WHERE id = %s;", (tenant_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if template_id and template_id > 0:
            cur.execute("SELECT id FROM rental_core.message_templates WHERE id = %s;", (template_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Template not found")
            cur.execute("""
                INSERT INTO rental_core.tenant_message_templates (tenant_id, template_id)
                VALUES (%s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET template_id = EXCLUDED.template_id
            """, (tenant_id, template_id))
        else:
            cur.execute("DELETE FROM rental_core.tenant_message_templates WHERE tenant_id = %s;", (tenant_id,))
        conn.commit()
        return {"status": "SUCCESS", "tenant_id": tenant_id, "template_id": template_id}
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


# --- 9c. WHATSAPP MESSAGE RENDER / SEND ---

@app.get("/api/tenants/{tenant_id}/whatsapp-message")
def tenant_whatsapp_message(tenant_id: int, billing_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Render the reminder message for a tenant+month and return phone + wa.me link."""
    conn = get_db()
    cur = conn.cursor()
    try:
        payload = wm.message_payload(cur, tenant_id, billing_month)
        return payload
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.post("/api/operations/send-whatsapp-reminders")
def trigger_whatsapp_reminders(background_tasks: BackgroundTasks):
    """Runs the automated WhatsApp reminder sender as a background task."""
    def run_sender():
        subprocess.run([sys.executable, "send_whatsapp_reminders.py"], check=False)

    background_tasks.add_task(run_sender)
    return {"status": "QUEUED", "message": "WhatsApp reminder sender triggered in background."}


@app.get("/api/whatsapp/send-log")
def get_whatsapp_send_log(limit: int = Query(200, ge=1, le=2000), batch_id: str = None):
    conn = get_db()
    cur = conn.cursor()
    if batch_id:
        cur.execute("""
            SELECT * FROM rental_core.whatsapp_send_log
            WHERE batch_id = %s ORDER BY id DESC LIMIT %s;
        """, (batch_id, limit))
    else:
        cur.execute("""
            SELECT * FROM rental_core.whatsapp_send_log
            ORDER BY id DESC LIMIT %s;
        """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.get("/api/whatsapp/last-batch")
def get_whatsapp_last_batch():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT batch_id, MAX(created_at) AS started_at,
               COUNT(*) FILTER (WHERE status = 'SENT') AS sent,
               COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
               COUNT(*) FILTER (WHERE status = 'SKIPPED') AS skipped,
               COUNT(*) AS total
        FROM rental_core.whatsapp_send_log
        GROUP BY batch_id ORDER BY MAX(created_at) DESC LIMIT 1;
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row or {"batch_id": None, "total": 0}


@app.get("/api/whatsapp/qr")
def get_whatsapp_qr(clean: bool = Query(False)):
    """Serve the current WhatsApp Web login QR image with no-cache headers so
    the browser always fetches the freshest frame (the QR rotates every ~20s).
    clean=true -> QR only (with quiet-zone padding), otherwise the full page."""
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "static", "whatsapp_qr_clean.png" if clean else "whatsapp_qr.png")
    if not _os.path.exists(path):
        raise HTTPException(status_code=404, detail="Run --login first to generate the QR image")
    from fastapi.responses import Response
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


# --- 9e. NEXT-DUE-DATE REMINDERS (per billing month) ---

class ReminderSet(BaseModel):
    tenant_id: int
    billing_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    reminder_date: str  # YYYY-MM-DD
    amount_due: Optional[float] = Field(default=None, ge=0)
    note: Optional[str] = None
    rem_type: Optional[str] = Field(default='rent', pattern="^(rent|deposit)$")
    paid_amount: Optional[float] = Field(default=None, ge=0)
    paid_date: Optional[str] = None


class ReminderSchedule(BaseModel):
    send_time: str = Field(default="10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    enabled: Optional[bool] = True


@app.get("/api/reminder-templates")
def get_reminder_templates():
    """Template master: DB overrides merged over built-in defaults."""
    from reminder_message import TEMPLATE_DEFAULTS
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, body, updated_at FROM rental_core.reminder_templates")
    rows = {r['key']: r for r in cur.fetchall()}
    cur.close(); conn.close()
    out = []
    for key, default_body in TEMPLATE_DEFAULTS.items():
        r = rows.get(key)
        out.append({
            "key": key,
            "body": (r['body'] if r else default_body),
            "is_custom": bool(r and r['body'].strip() != default_body.strip()),
            "updated_at": (str(r['updated_at'])[:19] if r else None),
        })
    return out


class ReminderTemplatesUpdate(BaseModel):
    templates: dict = Field(default_factory=dict)  # {key: body}


@app.put("/api/reminder-templates")
def put_reminder_templates(data: ReminderTemplatesUpdate):
    from reminder_message import TEMPLATE_DEFAULTS, invalidate_template_cache
    conn = get_db()
    cur = conn.cursor()
    saved = 0
    for key, body in (data.templates or {}).items():
        if key not in TEMPLATE_DEFAULTS:
            continue  # never persist unknown keys
        cur.execute("""
            INSERT INTO rental_core.reminder_templates (key, body, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET body = EXCLUDED.body, updated_at = now()
        """, (key, str(body)))
        saved += 1
    conn.commit()
    cur.close(); conn.close()
    invalidate_template_cache()
    return {"status": "SUCCESS", "saved": saved}


@app.post("/api/reminder-templates/reset")
def reset_reminder_template(key: str):
    """Delete override -> falls back to built-in default."""
    from reminder_message import invalidate_template_cache
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM rental_core.reminder_templates WHERE key = %s", (key,))
    deleted = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    invalidate_template_cache()
    return {"status": "SUCCESS", "reset": bool(deleted)}


@app.get("/api/reminders/schedule")
def get_reminder_schedule():
    """Admin-set master auto-send time + enabled flag."""
    return {
        "send_time": rm.send_time_setting(),
        "enabled": rm.sends_enabled(),
    }


@app.put("/api/reminders/schedule")
def set_reminder_schedule(data: ReminderSchedule):
    """Persist the admin-set master send time + enabled flag. The systemd
    sender timer is rescheduled by the daily 00:00 sync service
    (tenant-due-reminders-sync) — no privileged action happens from the GUI."""
    rm.set_setting("reminder_send_time", data.send_time)
    rm.set_setting("reminder_send_enabled", "Y" if data.enabled else "N")
    return {
        "status": "SUCCESS",
        "send_time": data.send_time,
        "enabled": data.enabled,
    }


@app.get("/api/reminders")
def list_reminders(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Calendar-month reminder view: every reminder whose DUE DATE falls in this
    month, regardless of billing_month. Shows both rent and deposit reminders."""
    year, mon = int(month[:4]), int(month[5:7])
    rows = rm.month_reminders(year, mon)
    return _utc_tagged(rows)


@app.get("/api/reminders/tenants")
def reminders_tenants(billing_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Active tenants with an outstanding balance for `billing_month`, plus the
    currently ACTIVE reminder date (if any). Used by the "set reminder" form."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT t.id, t.tenant_name, t.phone_number, r.display_address,
               ml.new_baki,
               ar.reminder_date AS active_reminder_date,
               ar.id AS active_reminder_id
        FROM rental_core.monthly_ledgers ml
        JOIN rental_core.tenants t ON ml.tenant_id = t.id
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        LEFT JOIN rental_core.tenant_reminders ar
               ON ar.tenant_id = t.id AND ar.billing_month = %s AND ar.status = 'ACTIVE'
        WHERE ml.billing_month = %s AND t.is_active = 'Y'
          AND COALESCE(ml.new_baki, 0.00) > 0
        ORDER BY r.display_address, t.tenant_name;
    """, (billing_month, billing_month))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.post("/api/reminders")
def set_reminder(data: ReminderSet):
    """Set/update a tenant's next due date for one billing month.

    Multiple dates per tenant+month are allowed (history kept): the newest row
    becomes ACTIVE and any earlier ACTIVE rows for the same tenant+month are
    flipped to SUPERSEDED. A NOTIFY message ("Your New Next Due Date is : ...")
    is queued so the sender dispatches it on its next run.
    """
    from datetime import datetime, timedelta as _dt
    try:
        # Prepend type tag to note for filtering
        if data.rem_type == 'deposit' and data.note and 'eposit' not in (data.note or ''):
            data.note = ('Deposit — ' + data.note) if data.note else 'Deposit due'
        elif data.rem_type == 'rent' and data.note and 'ent' not in (data.note or '').lower():
            data.note = ('Rent — ' + data.note) if data.note else ''
        reminder_date = _dt.strptime(data.reminder_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="reminder_date must be YYYY-MM-DD")

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM rental_core.tenants WHERE id = %s;", (data.tenant_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Tenant not found")

        if data.amount_due is None:
            cur.execute("""
                SELECT new_baki FROM rental_core.monthly_ledgers
                WHERE tenant_id = %s AND billing_month = %s
            """, (data.tenant_id, data.billing_month))
            row = cur.fetchone()
            amount_due = row['new_baki'] if row else 0.0
        else:
            amount_due = data.amount_due

        # If an ACTIVE reminder of the SAME TYPE already exists for the same
        # date, treat this save as an AMOUNT/NOTE correction: update that row
        # in place and keep its lifecycle stamps (notify/d1/due). Re-creating
        # the row reset its stamps and made it instantly D1/DUE-eligible again
        # — the root cause of duplicate reminder storms.
        cur.execute("""
            SELECT id FROM rental_core.tenant_reminders
             WHERE tenant_id = %s AND billing_month = %s AND status = 'ACTIVE'
               AND COALESCE(rem_type, 'rent') = %s AND reminder_date = %s
             ORDER BY id LIMIT 1
        """, (data.tenant_id, data.billing_month, data.rem_type or 'rent', reminder_date))
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE rental_core.tenant_reminders
                   SET amount_due = %s, note = %s, updated_at = now()
                 WHERE id = %s
                RETURNING *
            """, (amount_due, data.note or "", existing['id']))
            reminder = cur.fetchone()
        else:
            # supersede previous ACTIVE reminders of the SAME TYPE only
            # (a deposit due-date must not kill an ACTIVE rent reminder and
            # vice-versa — both can legitimately coexist for one month)
            cur.execute("""
                UPDATE rental_core.tenant_reminders
                   SET status = 'SUPERSEDED', updated_at = now()
                 WHERE tenant_id = %s AND billing_month = %s AND status = 'ACTIVE'
                   AND COALESCE(rem_type, 'rent') = %s
            """, (data.tenant_id, data.billing_month, data.rem_type or 'rent'))

            # cancel now-orphaned QUEUED notifications of superseded reminders so
            # no phantom QUEUED entries linger in the send log
            cur.execute("""
                UPDATE rental_core.reminder_send_log l
                   SET status = 'CANCELLED', error = 'superseded by newer reminder of same type'
                  FROM rental_core.tenant_reminders r
                 WHERE l.reminder_id = r.id AND l.status = 'QUEUED'
                   AND r.status = 'SUPERSEDED' AND r.tenant_id = %s
                   AND COALESCE(r.rem_type, 'rent') = %s
            """, (data.tenant_id, data.rem_type or 'rent'))

            cur.execute("""
                INSERT INTO rental_core.tenant_reminders
                (tenant_id, billing_month, reminder_date, amount_due, note, rem_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (data.tenant_id, data.billing_month, reminder_date, amount_due,
                  data.note or "", data.rem_type or 'rent'))
            reminder = cur.fetchone()

        # queue NOTIFY message (type-aware + payment ack)
        payload = rm.reminder_payload(cur, reminder,
                                      rem_type=data.rem_type or 'rent',
                                      paid_amount=data.paid_amount,
                                      paid_date=data.paid_date)
        cur.execute("""
            INSERT INTO rental_core.reminder_send_log
            (batch_id, reminder_id, tenant_id, phone_number, send_type,
             message, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED')
        """, (datetime.now().strftime("%Y%m%d%H%M%S") + "_set",
              reminder['id'], reminder['tenant_id'], payload['phone_number'],
              'NOTIFY', payload['notify']))
        conn.commit()
        return {
            "status": "SUCCESS",
            "reminder": reminder,
            "message": payload["notify"],
            "wa_link": payload["wa_link"],
            "notify_queued": True,
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.put("/api/reminders/{reminder_id}")
def update_reminder(reminder_id: int, data: ReminderSet):
    """Edit the date/amount of an existing reminder (keeps one row)."""
    from datetime import datetime, timedelta as _dt
    try:
        # Prepend type tag to note for filtering
        if data.rem_type == 'deposit' and data.note and 'eposit' not in (data.note or ''):
            data.note = ('Deposit — ' + data.note) if data.note else 'Deposit due'
        elif data.rem_type == 'rent' and data.note and 'ent' not in (data.note or '').lower():
            data.note = ('Rent — ' + data.note) if data.note else ''
        reminder_date = _dt.strptime(data.reminder_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="reminder_date must be YYYY-MM-DD")
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE rental_core.tenant_reminders
               SET reminder_date = %s, amount_due = %s, note = %s,
                   status = 'ACTIVE', updated_at = now()
             WHERE id = %s
             RETURNING *
        """, (reminder_date, data.amount_due, data.note or "", reminder_id))
        reminder = cur.fetchone()
        if reminder is None:
            raise HTTPException(status_code=404, detail="Reminder not found")
        # supersede any OTHER active reminders for the same tenant+month
        cur.execute("""
            UPDATE rental_core.tenant_reminders
               SET status = 'SUPERSEDED', updated_at = now()
             WHERE tenant_id = %s AND billing_month = %s AND status = 'ACTIVE'
               AND id <> %s
        """, (reminder['tenant_id'], reminder['billing_month'], reminder_id))
        payload = rm.reminder_payload(cur, reminder)
        cur.execute("""
            INSERT INTO rental_core.reminder_send_log
            (batch_id, reminder_id, tenant_id, phone_number, send_type,
             message, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED')
        """, (datetime.now().strftime("%Y%m%d%H%M%S") + "_set",
              reminder['id'], reminder['tenant_id'], payload['phone_number'],
              'NOTIFY', payload['notify']))
        conn.commit()
        return {
            "status": "SUCCESS",
            "reminder": reminder,
            "message": payload["notify"],
            "wa_link": payload["wa_link"],
            "notify_queued": True,
        }
    except HTTPException:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise
    except Exception as e:
        try:
            if not conn.closed:
                conn.rollback()
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            conn.close()
        except:
            pass


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder(reminder_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM rental_core.tenant_reminders WHERE id = %s RETURNING id;",
                    (reminder_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Reminder not found")
        conn.commit()
        return {"status": "SUCCESS", "deleted_id": reminder_id}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


@app.post("/api/operations/send-due-reminders")
def trigger_due_reminders(background_tasks: BackgroundTasks):
    """Runs the automated next-due-date reminder sender as a background task."""
    def run_sender():
        subprocess.run([sys.executable, "send_due_reminders.py", "--kind", "instant"], check=False)

    background_tasks.add_task(run_sender)
    return {"status": "QUEUED", "message": "Next-due-date reminder sender triggered in background."}


@app.get("/api/reminders/run-status")
def reminders_run_status():
    """Live status of the reminder sender process (instant or scheduled).
    The sender itself maintains .sender_state.json; GUI polls this."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_state.json")
    try:
        with open(path) as f:
            st = json.load(f)
    except Exception:
        return {"running": False, "status": "idle"}

    running = st.get("status") == "running"
    if running and st.get("pid"):
        try:
            os.kill(int(st["pid"]), 0)  # liveness check (no signal sent)
        except Exception:
            running = False
            st["status"] = "crashed"
    return {
        "running": running,
        "run_id": st.get("run_id"),
        "kind": st.get("kind"),
        "pid": st.get("pid"),
        "status": st.get("status"),
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "updated_at": st.get("updated_at"),
        "summary": st.get("summary"),
        "lines": st.get("lines", [])[-80:],
    }


def _utc_tagged(rows):
    """Tag naive UTC datetimes with 'Z' so browsers convert to local/IST."""
    import datetime as _dt
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, _dt.datetime):
                d[k] = v.isoformat() + 'Z'
        out.append(d)
    return out


@app.get("/api/reminders/send-log")
def get_reminder_send_log(limit: int = Query(200, ge=1, le=2000), batch_id: str = None):
    conn = get_db()
    cur = conn.cursor()
    # NOTE: screenshot column intentionally omitted from base list to speed up
    # the initial table load. Full image is fetched on-demand via the
    # dedicated endpoint below.
    if batch_id:
        cur.execute("""
            SELECT l.id, l.batch_id, l.reminder_id, l.tenant_id, l.phone_number,
                   l.send_type, l.status, l.error, l.message, l.created_at,
                   l.sent_at, t.tenant_name, r.display_address
            FROM rental_core.reminder_send_log l
            LEFT JOIN rental_core.tenants t ON l.tenant_id = t.id
            LEFT JOIN rental_core.rooms r ON t.room_id = r.id
            WHERE l.batch_id = %s ORDER BY l.id DESC LIMIT %s;
        """, (batch_id, limit))
    else:
        cur.execute("""
            SELECT l.id, l.batch_id, l.reminder_id, l.tenant_id, l.phone_number,
                   l.send_type, l.status, l.error, l.message, l.created_at,
                   l.sent_at, t.tenant_name, r.display_address
            FROM rental_core.reminder_send_log l
            LEFT JOIN rental_core.tenants t ON l.tenant_id = t.id
            LEFT JOIN rental_core.rooms r ON t.room_id = r.id
            ORDER BY l.id DESC LIMIT %s;
        """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return _utc_tagged(rows)


@app.get("/api/reminders/send-log-screenshot/{log_id}")
def get_send_log_screenshot(log_id: int):
    """Return the screenshot base64 data for a single send-log entry.

    Used by the Reminder Send Log table: the "View Screenshot" button
    fetches exactly one image instead of sending every screenshot base64
    string in the initial bulk load.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT screenshot FROM rental_core.reminder_send_log WHERE id = %s",
        (log_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row.get("screenshot"):
        return {"screenshot": None}
    return {"screenshot": row["screenshot"]}


@app.get("/api/reminders/last-batch")
def get_reminder_last_batch():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT batch_id, MAX(created_at) AS started_at,
               COUNT(*) FILTER (WHERE status = 'SENT') AS sent,
               COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
               COUNT(*) FILTER (WHERE status = 'SKIPPED') AS skipped,
               COUNT(*) FILTER (WHERE status = 'QUEUED') AS queued,
               COUNT(*) AS total
        FROM rental_core.reminder_send_log
        GROUP BY batch_id ORDER BY MAX(created_at) DESC LIMIT 1;
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row or {"batch_id": None, "total": 0}