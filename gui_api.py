import os
import re
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
    previous_tenant_id: Optional[int] = Field(default=None, ge=1)
    country_code: Optional[str] = Field(default="91", pattern="^(91|977)$")

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

class PaymentUpdate(BaseModel):
    payment_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-\d{2}$")
    amount: Optional[float] = Field(default=None, ge=0)
    payment_mode: Optional[str] = Field(default=None, pattern="^(CASH|UPI|BANK_TRANSFER|CARD|CHEQUE|ADJUSTMENT)$")
    payment_type: Optional[str] = Field(default=None, pattern="^(RENT|DEPOSIT|SETTLEMENT|REFUND)$")
    billing_month: Optional[str] = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    note: Optional[str] = None


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
def get_dashboard_stats(month: str = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    conn = get_db()
    cur = conn.cursor()
    execution_month = month or current_billing_period()

    cur.execute("SELECT COUNT(*) as count FROM rental_core.property_masters;")
    props = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.rooms;")
    total_rooms = cur.fetchone()['count']

    cur.execute("SELECT COUNT(DISTINCT room_id) as count FROM rental_core.tenants WHERE is_active = 'Y';")
    occupied_rooms = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.tenants WHERE is_active = 'Y';")
    active_tenants = cur.fetchone()['count']

    cur.execute("""
        SELECT COALESCE(SUM(CASE WHEN payment_status = 'Settled' THEN 0 ELSE new_baki END), 0.00) as total_outstanding,
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

    # Collected split by payment mode (RENT cash-flows only, same source as
    # the jama sync; only modes with collection > 0 are returned).
    cur.execute("""
        SELECT payment_mode AS mode, COALESCE(SUM(amount), 0.00) AS amount
        FROM rental_core.tenant_payments
        WHERE billing_month = %s AND payment_type = 'RENT'
        GROUP BY payment_mode
        HAVING COALESCE(SUM(amount), 0.00) > 0
        ORDER BY amount DESC;
    """, (execution_month,))
    collections_by_mode = [
        {"mode": r['mode'], "amount": float(r['amount'] or 0)}
        for r in cur.fetchall()
    ]

    cur.execute("""
        SELECT COALESCE(SUM(gross_baki), 0.00) AS fixed_due_total,
                COALESCE(SUM(CASE WHEN payment_status = 'Settled' THEN 0 WHEN new_baki > 0 THEN new_baki ELSE 0 END), 0.00) AS current_due_total
        FROM rental_core.monthly_ledgers
        WHERE billing_month = %s;
    """, (execution_month,))
    due_row = cur.fetchone()
    fixed_due_total = due_row['fixed_due_total']
    current_due_total = due_row['current_due_total']

    cur.execute("""
        SELECT pm.apartment_name,
                COALESCE(SUM(CASE WHEN ml.payment_status = 'Settled' THEN 0 ELSE ml.new_baki END), 0.00) AS outstanding,
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
        SELECT COALESCE(SUM(CASE WHEN payment_status = 'Settled' THEN 0 ELSE new_baki END), 0.00) AS older_outstanding
        FROM rental_core.monthly_ledgers
        WHERE billing_month < %s;
    """, (execution_month,))
    older_outstanding = cur.fetchone()['older_outstanding']

    # Settlement KPIs for current execution month
    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'SETTLEMENT_EXTRA' THEN p.amount ELSE 0 END), 0) AS settlement_inward,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_UTILIZED' THEN p.amount ELSE 0 END), 0) AS settlement_deposit_utilized,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_REFUNDED' THEN p.amount ELSE 0 END), 0) AS settlement_outward
        FROM rental_core.tenant_payments p
        JOIN rental_core.tenants t ON p.tenant_id = t.id
        JOIN rental_core.rooms r ON t.room_id = r.id
        WHERE p.billing_month = %s
          AND p.payment_type IN ('SETTLEMENT', 'REFUND')
          AND p.payment_subtype IS NOT NULL;
    """, (execution_month,))
    settlement_row = cur.fetchone()
    settlement_inward = float(settlement_row['settlement_inward'] or 0)
    settlement_deposit_utilized = float(settlement_row['settlement_deposit_utilized'] or 0)
    settlement_outward = float(settlement_row['settlement_outward'] or 0)
    settlement_net = settlement_inward - settlement_outward

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
        "collections_by_mode": collections_by_mode,
        "payment_recovery_pct": recovery_pct,
        "billed_total": float(billed_total),
        "fixed_due_total": float(fixed_due_total),
        "current_due_total": float(current_due_total),
        "carried_forward": float(carried_forward),
        "older_outstanding": float(older_outstanding),
        "outstanding_by_property": [
            {"apartment_name": p['apartment_name'], "outstanding": float(p['outstanding']), "units": p['units']}
            for p in outstanding_by_property
        ],
        # Settlement KPIs
        "settlement_inward": settlement_inward,
        "settlement_deposit_utilized": settlement_deposit_utilized,
        "settlement_outward": settlement_outward,
        "settlement_net": settlement_net,
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
                COALESCE(SUM(CASE WHEN payment_status = 'Settled' THEN 0 WHEN new_baki > 0 THEN new_baki ELSE 0 END), 0) AS outstanding_pending,
                COALESCE(SUM(CASE WHEN payment_status = 'Settled' THEN 0 ELSE new_baki END), 0) AS outstanding_net,
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
               COALESCE(SUM(CASE WHEN ml.payment_status = 'Settled' THEN 0 WHEN ml.new_baki > 0 THEN ml.new_baki ELSE 0 END), 0) AS outstanding_pending,
               COALESCE(SUM(CASE WHEN ml.payment_status = 'Settled' THEN 0 ELSE ml.new_baki END), 0) AS outstanding_net,
               COALESCE(SUM(ml.bhadu_applied), 0) AS rent_billed,
               COALESCE(SUM(COALESCE(ml.maintenance_manual, ml.maintenance_applied)), 0) AS maintenance_billed,
               COALESCE(SUM(COALESCE(ml.light_bill_manual, ml.light_bill) + COALESCE(ml.gas_bill_manual, ml.gas_bill)), 0) AS utility_billed,
               COUNT(DISTINCT ml.tenant_id) AS tenant_count,
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
            "rent_billed": float(r['rent_billed'] or 0),
            "maintenance_billed": float(r['maintenance_billed'] or 0),
            "utility_billed": float(r['utility_billed'] or 0),
            "tenant_count": int(r['tenant_count'] or 0),
            "units": r['units'],
            "recovery_pct": round(collected / gross_billed * 100, 1) if gross_billed else 0.0,
        })
    cur.close()
    conn.close()
    return {"rows": rows}


@app.get("/api/reports/daily-progress")
def report_daily_progress(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                          property_id: int = None):
    """Day-to-day collection progress inside one billing month.

    Per calendar day: cash collected that day (RENT + DEPOSIT + settlement
    cash; ADJUSTMENT and outward REFUND excluded) plus running cumulative.
    `target` = the month's gross billed (same basis as the monthly chart).
    Optional property filter follows the payment's room.
    """
    import calendar as _cal
    conn = get_db()
    cur = conn.cursor()
    try:
        datetime.strptime(month, "%Y-%m")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month, use YYYY-MM")
    _y, _m = map(int, month.split('-'))
    _ndays = _cal.monthrange(_y, _m)[1]
    _pfilter = "AND p.room_id IN (SELECT id FROM rental_core.rooms WHERE master_id = %s)" if property_id else ""
    _pargs = [f"{month}-01", f"{month}-01"]
    if property_id:
        _pargs.append(property_id)
    cur.execute(f"""
        SELECT d::date AS day, COALESCE(SUM(p.amount), 0) AS collected
        FROM generate_series(%s::date, (%s::date + interval '1 month - 1 day')::date, '1 day') d
        LEFT JOIN rental_core.tenant_payments p ON p.payment_date = d::date
          AND COALESCE(p.payment_mode, '') <> 'ADJUSTMENT'
          AND p.payment_type <> 'REFUND'
          {_pfilter}
        GROUP BY d ORDER BY d
    """, _pargs)
    days = []
    _cum = 0.0
    for r in cur.fetchall():
        _c = float(r['collected'] or 0)
        _cum += _c
        days.append({"day": r['day'].day, "label": str(r['day'].day),
                     "collected_day": round(_c, 2), "collected_cum": round(_cum, 2)})
    _targs = [month]
    _tfilter = ""
    if property_id:
        _tfilter = "AND ml.room_id IN (SELECT id FROM rental_core.rooms WHERE master_id = %s)"
        _targs.append(property_id)
    cur.execute(f"""
        SELECT COALESCE(SUM(ml.gross_baki), 0) AS target,
               COALESCE(SUM(ml.jama), 0) AS collected
        FROM rental_core.monthly_ledgers ml
        WHERE ml.billing_month = %s {_tfilter}
    """, _targs)
    _t = cur.fetchone()
    target = float(_t['target'] or 0)
    total = float(_t['collected'] or 0)
    try:
        label = datetime.strptime(month, "%Y-%m").strftime("%b %Y")
    except Exception:
        label = month
    cur.close()
    conn.close()
    return {"billing_month": month, "month_label": label, "days": days,
            "day_count": _ndays, "target": round(target, 2),
            "total_collected": round(total, 2),
            "recovery_pct": round(total / target * 100, 1) if target else 0.0}


@app.get("/api/reports/deposit-summary")
def report_deposit_summary():
    """Property-wise deposit summary with per-tenant breakdown.

    Shows: required, received, utilized, refunded, balance, extra_settlement per property.
    Balance = Received - Utilized - Refunded (floored at 0).
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            pm.id AS property_id,
            pm.apartment_name,
            pm.plot_number,
            t.id AS tenant_id,
            t.tenant_name,
            t.room_id,
            t.security_deposit_applied AS required_deposit,
            r.display_address,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_RECEIVED' THEN p.amount ELSE 0 END), 0) AS received,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_UTILIZED' THEN p.amount ELSE 0 END), 0) AS utilized,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_REFUNDED' THEN p.amount ELSE 0 END), 0) AS refunded,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'SETTLEMENT_EXTRA' THEN p.amount ELSE 0 END), 0) AS extra_settlement
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        LEFT JOIN rental_core.tenant_payments p
            ON p.tenant_id = t.id
            AND p.payment_type IN ('DEPOSIT', 'SETTLEMENT', 'REFUND')
            AND p.payment_subtype IS NOT NULL
        GROUP BY pm.id, pm.apartment_name, pm.plot_number, t.id, t.tenant_name, t.room_id, t.security_deposit_applied, r.display_address
        ORDER BY pm.apartment_name, r.display_address;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    # Aggregate by property
    by_property = {}
    for r in rows:
        pid = r['property_id']
        if pid not in by_property:
            by_property[pid] = {
                "property_id": pid,
                "apartment_name": r['apartment_name'],
                "plot_number": r['plot_number'],
                "total_required": 0.0,
                "total_received": 0.0,
                "total_utilized": 0.0,
                "total_refunded": 0.0,
                "total_extra": 0.0,
                "tenants": []
            }
        prop = by_property[pid]
        required = float(r['required_deposit'] or 0)
        received = float(r['received'] or 0)
        utilized = float(r['utilized'] or 0)
        refunded = float(r['refunded'] or 0)
        extra = float(r['extra_settlement'] or 0)
        balance = max(0.0, received - utilized - refunded)

        prop["total_required"] += required
        prop["total_received"] += received
        prop["total_utilized"] += utilized
        prop["total_refunded"] += refunded
        prop["total_extra"] += extra
        prop["tenants"].append({
            "tenant_id": r['tenant_id'],
            "tenant_name": r['tenant_name'],
            "room_id": r['room_id'],
            "display_address": r['display_address'],
            "required": required,
            "received": received,
            "utilized": utilized,
            "refunded": refunded,
            "extra": extra,
            "balance": balance,
        })

    # Grand totals
    grand = {
        "total_required": sum(p["total_required"] for p in by_property.values()),
        "total_received": sum(p["total_received"] for p in by_property.values()),
        "total_utilized": sum(p["total_utilized"] for p in by_property.values()),
        "total_refunded": sum(p["total_refunded"] for p in by_property.values()),
        "total_extra": sum(p["total_extra"] for p in by_property.values()),
    }
    grand["total_balance"] = max(0.0, grand["total_received"] - grand["total_utilized"] - grand["total_refunded"])

    return {
        "properties": list(by_property.values()),
        "grand_total": grand,
    }


@app.get("/api/reports/settlements")
def report_settlements(
    from_date: str = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    property_id: int = Query(None, ge=1),
    payment_type: str = Query(None, pattern="^(SETTLEMENT|REFUND)$"),
):
    """Settlement report with filters: date range, property, type (SETTLEMENT/REFUND).

    Returns individual settlement transactions with sub-type breakdown.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        where = ["p.payment_type IN ('SETTLEMENT', 'REFUND')", "p.payment_subtype IS NOT NULL"]
        params = []

        if from_date:
            where.append("p.payment_date >= %s")
            params.append(from_date)
        if to_date:
            where.append("p.payment_date <= %s")
            params.append(to_date)
        if property_id:
            where.append("pm.id = %s")
            params.append(property_id)
        if payment_type:
            where.append("p.payment_type = %s")
            params.append(payment_type)

        where_clause = " AND ".join(where)

        cur.execute(f"""
            SELECT
                p.id,
                p.payment_date,
                p.payment_type,
                p.payment_subtype,
                p.amount,
                p.payment_mode,
                p.note,
                p.billing_month,
                t.id AS tenant_id,
                t.tenant_name,
                r.display_address,
                pm.id AS property_id,
                pm.apartment_name
            FROM rental_core.tenant_payments p
            JOIN rental_core.tenants t ON p.tenant_id = t.id
            JOIN rental_core.rooms r ON t.room_id = r.id
            JOIN rental_core.property_masters pm ON r.master_id = pm.id
            WHERE {where_clause}
            ORDER BY p.payment_date DESC, p.id DESC;
        """, params)
        rows = cur.fetchall()

        # Aggregate by property
        by_property = {}
        grand_inward = grand_utilized = grand_outward = grand_extra = 0.0

        for r in rows:
            pid = r['property_id']
            if pid not in by_property:
                by_property[pid] = {
                    "property_id": pid,
                    "apartment_name": r['apartment_name'],
                    "total_inward": 0.0,
                    "total_utilized": 0.0,
                    "total_outward": 0.0,
                    "total_extra": 0.0,
                    "transactions": []
                }
            prop = by_property[pid]
            amount = float(r['amount'])
            subtype = r['payment_subtype']

            entry = {
                "id": r['id'],
                "date": str(r['payment_date']),
                "tenant_id": r['tenant_id'],
                "tenant_name": r['tenant_name'],
                "display_address": r['display_address'],
                "type": r['payment_type'],
                "subtype": subtype,
                "amount": amount,
                "mode": r['payment_mode'],
                "note": r['note'],
                "billing_month": r['billing_month'],
            }
            prop["transactions"].append(entry)

            if subtype == 'SETTLEMENT_EXTRA':
                prop["total_inward"] += amount
                grand_inward += amount
            elif subtype == 'DEPOSIT_UTILIZED':
                prop["total_utilized"] += amount
                grand_utilized += amount
            elif subtype == 'DEPOSIT_REFUNDED':
                prop["total_outward"] += amount
                grand_outward += amount
            elif subtype == 'DEPOSIT_RECEIVED':
                pass  # not part of settlement report

        # Summary
        summary = {
            "grand_inward": grand_inward,
            "grand_utilized": grand_utilized,
            "grand_outward": grand_outward,
            "grand_net": grand_inward - grand_outward,
        }

        return {
            "from_date": from_date,
            "to_date": to_date,
            "property_id": property_id,
            "payment_type": payment_type,
            "summary": summary,
            "properties": list(by_property.values()),
        }
    except Exception as e:
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


# --- 2. PROPERTY MASTERS & ROOMS ---
@app.get("/api/dashboard/kpi")
def get_dashboard_kpi(month: str = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Breakdown data for the clickable KPI cards on the executive dashboard."""
    conn = get_db()
    cur = conn.cursor()
    execution_month = month or current_billing_period()

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
               t.country_code,
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
               t.country_code,
               r.display_address,
               pm.apartment_name,
                ml.gross_baki,
                ml.jama,
                ml.new_baki,
                ml.payment_status,
                ml.settled_estimate,
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
               t.country_code,
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
            t.country_code,
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
        ORDER BY COALESCE(r.display_address, '') ASC, t.tenant_name ASC;
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

    try:
        _hist_all = tenant.get('settlement_history') or []
        if isinstance(_hist_all, str):
            _hist_all = json.loads(_hist_all)
    except Exception:
        _hist_all = []

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
            SELECT id, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id
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
                    "payment_subtype": p.get('payment_subtype'),
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
                "settled_estimate": bool(lrow.get('settled_estimate')),
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
                "settled_estimate": False,
                "jama_date": '0', "light_bill_type": 'NORMAL',
                "light_bill_actual": None, "light_bill_manual": None,
                "gas_bill_actual": None, "gas_bill_manual": None,
                "maintenance_actual": None, "maintenance_manual": None,
                "common_misc_actual": None, "common_misc_manual": None,
                "manual_override": False,
                "settlement_summary": None,
                "settlement_basis": False,
                "estimated_light": None,
                "estimated_gas": None,
            })

        # Settlement summary for the month popup (recorded facts only).
        _settle_pays = [p for p in pay_rows if p['payment_type'] in ('SETTLEMENT', 'REFUND')]
        _entries = [h for h in _hist_all if isinstance(h, dict) and h.get('billing_month') == bm]
        if _settle_pays:
            _on_est = bool(lrow and lrow.get('settled_estimate')) or any(h.get('used_estimates') for h in _entries)
            # Live settlement position (remaining + direction) so rows can show
            # the true "still due / refundable" alongside the monthly Baki.
            # Computed only for months with settlement activity.
            try:
                _, _ss = _settlement_month_figures(cur, tenant_id, bm)
                _rem_rec = round(max(0.0, float(_ss['remaining_receivable'])), 2)
                _rem_pay = round(max(0.0, float(_ss['remaining_payable'])), 2)
                _ss_dir = 'receivable' if _ss['is_receivable'] else ('payable' if _ss['is_payable'] else 'none')
            except Exception:
                _rem_rec = _rem_pay = 0.0
                _ss_dir = 'none'
            base["settlement_summary"] = {
                "settled_amount": round(sum(float(p['amount'] or 0) for p in pay_rows if p['payment_type'] == 'SETTLEMENT' and p.get('payment_subtype') in (None, 'SETTLEMENT_EXTRA')), 2),
                "utilized_amount": round(sum(float(p['amount'] or 0) for p in pay_rows if p.get('payment_subtype') == 'DEPOSIT_UTILIZED'), 2),
                "refunded_amount": round(sum(float(p['amount'] or 0) for p in pay_rows if p['payment_type'] == 'REFUND' and p.get('payment_subtype') in (None, 'DEPOSIT_REFUNDED')), 2),
                "remaining_receivable": _rem_rec,
                "remaining_payable": _rem_pay,
                "direction": _ss_dir,
                "on_estimates": _on_est,
                "est_light": next((h.get('est_light') for h in reversed(_entries) if h.get('est_light') is not None), None),
                "est_gas": next((h.get('est_gas') for h in reversed(_entries) if h.get('est_gas') is not None), None),
                "entries": [
                    {"payment_type": p['payment_type'], "payment_subtype": p.get('payment_subtype'),
                     "amount": float(p['amount'] or 0),
                     "payment_date": str(p['payment_date']), "payment_mode": p['payment_mode'],
                     "note": p['note']}
                    for p in sorted(_settle_pays, key=lambda p: (str(p['payment_date']), p['id']))
                ],
            }
        else:
            base["settlement_summary"] = None

        # Settlement-basis overlay: a Settled closing month whose actual ledger
        # was never prepared shows blank zeros from its skeleton row. Replay the
        # settlement-basis breakdown instead (history snapshot preferred, fresh
        # recompute fallback for pre-snapshot settlements). Only blank fields
        # are overlaid — stored/manual values always win. Chain facts
        # (prev/jama) stay live; totals re-derive from them.
        base["settlement_basis"] = False
        base["estimated_light"] = None
        base["estimated_gas"] = None
        if lrow and lrow['payment_status'] == 'Settled' and _entries:
            _snap_e = next((h for h in reversed(_entries) if isinstance(h.get('brk'), dict)), None)
            if _snap_e is not None:
                _brk = _snap_e['brk']
                _eff = {k: float(_brk.get(k) or 0) for k in ('bhadu', 'light', 'gas', 'maint', 'common')}
                _est_l = _snap_e.get('est_light')
                _est_g = _snap_e.get('est_gas')
            else:
                try:
                    _le = datetime.strptime(_entries[-1].get('lease_end_date', f"{bm}-28")[:10], "%Y-%m-%d").date()
                    _s2 = _compute_settlement(cur, tenant_id, _le, bm,
                                              {'bhadu': None, 'light': None, 'gas': None,
                                               'maintenance': None, 'common': None})
                    _eff = {'bhadu': float(_s2['bhadu']), 'light': float(_s2['light']),
                            'gas': float(_s2['gas']), 'maint': float(_s2['maint']),
                            'common': float(_s2['common'])}
                    _est_l = float(_s2['light']) if _s2['light_from_est'] else None
                    _est_g = float(_s2['gas']) if _s2['gas_from_est'] else None
                except Exception:
                    _eff = None
            if _eff is not None:
                _man_l = lrow.get('light_bill_manual')
                _man_g = lrow.get('gas_bill_manual')
                _man_m = lrow.get('maintenance_manual')
                _man_c = lrow.get('common_misc_manual')
                _b = float(lrow['bhadu_applied'] or 0) or _eff['bhadu']
                _l = float(_man_l) if _man_l is not None else (float(lrow['light_bill'] or 0) or _eff['light'])
                _g = float(_man_g) if _man_g is not None else (float(lrow['gas_bill'] or 0) or _eff['gas'])
                _m = float(_man_m) if _man_m is not None else (float(lrow['maintenance_applied'] or 0) or _eff['maint'])
                _c = float(_man_c) if _man_c is not None else (float(lrow['common_misc_expenses'] or 0) or _eff['common'])
                _mt = round(_b + _l + _g + _m + _c, 2)
                _gb = round(base["prev_baki"] + _mt - base["prev_jama"], 2)
                base.update({
                    "bhadu_applied": _b, "light_bill": _l, "gas_bill": _g,
                    "maintenance_applied": _m, "common_misc_expenses": _c,
                    "month_total": _mt, "gross_baki": _gb,
                    "new_baki": round(_gb - base["jama"], 2),
                    "estimated_light": _est_l, "estimated_gas": _est_g,
                    "settlement_basis": True,
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

    # History maxima for light/gas: always fetched. Uncompiled months use
    # them as the utility defaults below, and a COMPILED closing month whose
    # light/gas sits at 0 (bill not yet generated — gas bills bi-monthly)
    # falls back to them too (see missing-bill substitution before month_total).
    # Per month the EFFECTIVE value counts (persisted manual override if any,
    # else the actual bill) — a corrected month contributes its correction.
    cur.execute("""
        SELECT COALESCE(light_bill_manual, light_bill, 0) AS lb,
               COALESCE(gas_bill_manual, gas_bill, 0) AS gb
        FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month < %s
        ORDER BY billing_month DESC LIMIT 6
    """, (tenant_id, billing_month))
    _hist = cur.fetchall()
    if _hist:
        _histmax_light = max(float(r['lb'] or 0) for r in _hist)
        _histmax_gas = max(float(r['gb'] or 0) for r in _hist)
    else:
        _histmax_light = 0.0
        _histmax_gas = 0.0

    # Estimates for uncompiled months: max of last up-to-6 billed months
    # (max of available months when fewer exist). Uncompiled = no row, or a
    # skeleton row whose bills were never entered. A compiled month always has
    # bhadu > 0, so its field zeros are genuine and must NOT be estimated over
    # — except a zero UTILITY bill on the closing month, handled separately.
    _row_bhadu = float(lrow['bhadu_applied'] or 0) if lrow else 0.0
    _uncompiled = (not lrow) or (_row_bhadu == 0)
    estimated_light = None
    estimated_gas = None
    if _uncompiled:
        estimated_light = _histmax_light
        estimated_gas = _histmax_gas

    custom_bhadu = customs.get('bhadu')
    custom_light = customs.get('light')
    custom_gas = customs.get('gas')
    custom_maintenance = customs.get('maintenance')
    custom_common = customs.get('common')

    # Defaults from tenant profile + history estimates (used when uncompiled).
    is_half = False
    base = float(tenant['base_rent_applied'] or tenant['default_base_rent'] or 3300)
    half_val = tenant.get('half_rent_applied') or tenant.get('room_half_rent') or tenant.get('default_half_rent')
    half_val = float(half_val) if half_val else 0
    is_half = _settlement_is_half_month(tenant.get('lease_start_date'), le_date, billing_month)
    half_computed = _settlement_half_rent(base, half_val)
    bhadu = half_computed if is_half else base
    maint = float(tenant['maintenance_applied'] or 0)
    common = 0.0
    light = estimated_light if estimated_light is not None else 0.0
    gas = estimated_gas if estimated_gas is not None else 0.0

    # Stored ledger values win when the month is compiled (manual overrides
    # already folded into the effective columns by the DB generated
    # expressions). For skeleton rows only persisted manuals are real — the
    # rest keeps the tenant/estimate defaults above.
    if lrow:
        if _row_bhadu > 0:
            bhadu = _row_bhadu
            light = float(lrow.get('light_bill_manual') if lrow.get('light_bill_manual') is not None else (lrow['light_bill'] or 0))
            gas = float(lrow.get('gas_bill_manual') if lrow.get('gas_bill_manual') is not None else (lrow['gas_bill'] or 0))
            maint = float(lrow.get('maintenance_manual') if lrow.get('maintenance_manual') is not None else (lrow['maintenance_applied'] or 0))
            common = float(lrow.get('common_misc_manual') if lrow.get('common_misc_manual') is not None else (lrow['common_misc_expenses'] or 0))
        else:
            if lrow.get('light_bill_manual') is not None:
                light = float(lrow['light_bill_manual'])
            if lrow.get('gas_bill_manual') is not None:
                gas = float(lrow['gas_bill_manual'])
            if lrow.get('maintenance_manual') is not None:
                maint = float(lrow['maintenance_manual'])
            if lrow.get('common_misc_manual') is not None:
                common = float(lrow['common_misc_manual'])
    # Preview customs win last, whether or not the closing month has a ledger
    # row yet. Final priority: custom > stored ledger (bills + persisted
    # manuals) > estimate. (POST persists customs first, then calls with
    # all-None customs, so this never double-applies on record.)
    if custom_bhadu is not None:
        bhadu = float(custom_bhadu)
    if custom_maintenance is not None:
        maint = float(custom_maintenance)
    if custom_common is not None:
        common = float(custom_common)
    if custom_light is not None:
        light = float(custom_light)
    if custom_gas is not None:
        gas = float(custom_gas)

    # Missing-bill estimates for the closing month: a compiled row whose
    # light or gas sits at 0 with no manual/custom override almost always
    # means the bill hasn't arrived yet (gas bills bi-monthly) — not genuine
    # zero consumption. Substitute the history max and mark provenance so the
    # badge, snapshot and true-up treat it as estimated. Empty history
    # (genuinely no such utility) and explicit manual/custom zeros stay zero.
    _substituted_light = False
    _substituted_gas = False
    _has_manual_light = bool(lrow and lrow.get('light_bill_manual') is not None)
    _has_manual_gas = bool(lrow and lrow.get('gas_bill_manual') is not None)
    if custom_light is None and not _has_manual_light and light == 0 and _histmax_light > 0:
        light = _histmax_light
        estimated_light = _histmax_light
        _substituted_light = True
    if custom_gas is None and not _has_manual_gas and gas == 0 and _histmax_gas > 0:
        gas = _histmax_gas
        estimated_gas = _histmax_gas
        _substituted_gas = True

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

    # Cash legs only: DEPOSIT_UTILIZED rows share the parent payment_type but
    # move no cash, so they must not reduce the remaining amounts. Legacy rows
    # (payment_subtype NULL) are cash and stay counted.
    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS settled
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND payment_type = 'SETTLEMENT'
          AND (payment_subtype IS NULL OR payment_subtype = 'SETTLEMENT_EXTRA')
    """, (tenant_id,))
    settled_row = cur.fetchone()
    total_settled = float(settled_row['settled'] or 0) if settled_row else 0.0

    cur.execute("""
        SELECT COALESCE(SUM(amount), 0) AS refunded
        FROM rental_core.tenant_payments
        WHERE tenant_id = %s AND payment_type = 'REFUND'
          AND (payment_subtype IS NULL OR payment_subtype = 'DEPOSIT_REFUNDED')
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

    # Estimate provenance: True when the value in play came from history
    # estimates — i.e. month uncompiled and neither customs nor persisted
    # manuals overrode that utility, OR a compiled closing month whose zero
    # bill was substituted above. Drives the 'Settled (Est.)' badge,
    # history snapshot, and later actual-vs-estimate true-up.
    _manual_light = lrow.get('light_bill_manual') if lrow else None
    _manual_gas = lrow.get('gas_bill_manual') if lrow else None
    light_from_est = bool((_uncompiled or _substituted_light) and custom_light is None and _manual_light is None)
    gas_from_est = bool((_uncompiled or _substituted_gas) and custom_gas is None and _manual_gas is None)
    used_estimates = bool(light_from_est or gas_from_est)

    return {
        "tenant": tenant,
        "lrow": lrow,
        "uncompiled": _uncompiled,
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
        "histmax_light": _histmax_light,
        "histmax_gas": _histmax_gas,
        "light_from_est": light_from_est,
        "gas_from_est": gas_from_est,
        "used_estimates": used_estimates,
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
        light_from_est = s['light_from_est']
        gas_from_est = s['gas_from_est']
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
            SELECT id, payment_type, payment_subtype, billing_month, payment_date, amount, payment_mode
            FROM rental_core.tenant_payments
            WHERE tenant_id = %s AND payment_type IN ('SETTLEMENT', 'REFUND')
            ORDER BY payment_date, id
        """, (tenant_id,))
        history = [
            {
                "id": h['id'],
                "payment_type": h['payment_type'],
                "payment_subtype": h.get('payment_subtype'),
                "billing_month": h['billing_month'],
                "payment_date": str(h['payment_date']),
                "amount": float(h['amount']),
                "payment_mode": h['payment_mode'],
            } for h in cur.fetchall()
        ]

        # Estimate true-up: this closing month was settled while light/gas were
        # estimated. If the actual bills have since arrived (month compiled),
        # report estimate-vs-actual variance so the residual is visible.
        estimate_trueup = None
        try:
            _hist_json = tenant.get('settlement_history') or []
            _snap = next((h for h in _hist_json
                          if isinstance(h, dict) and h.get('billing_month') == billing_month
                          and h.get('used_estimates')), None)
        except Exception:
            _snap = None
        if _snap is not None:
            _compiled_now = bool(lrow) and float(lrow['bhadu_applied'] or 0) > 0
            _est_l = _snap.get('est_light')
            _est_g = _snap.get('est_gas')
            if _compiled_now:
                _a_l = lrow.get('light_bill_actual')
                _a_l = float(_a_l) if _a_l is not None else float(lrow['light_bill'] or 0)
                _a_g = lrow.get('gas_bill_actual')
                _a_g = float(_a_g) if _a_g is not None else float(lrow['gas_bill'] or 0)
                _v_l = round(_a_l - _est_l, 2) if _est_l is not None else None
                _v_g = round(_a_g - _est_g, 2) if _est_g is not None else None
                _v_t = round(sum(v for v in (_v_l, _v_g) if v is not None), 2)
            else:
                _a_l = _a_g = _v_l = _v_g = _v_t = None
            estimate_trueup = {
                "settled_on_estimates": True,
                "est_light": float(_est_l) if _est_l is not None else None,
                "est_gas": float(_est_g) if _est_g is not None else None,
                "actuals_arrived": _compiled_now,
                "actual_light": _a_l,
                "actual_gas": _a_g,
                "var_light": _v_l,
                "var_gas": _v_g,
                "variance_total": _v_t,
                "matches": (_v_t is not None and abs(_v_t) <= 0.01),
            }

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
            f"Rent: ₹{_amt(bhadu)} | Light: ₹{_amt(light)}{' (est.)' if light_from_est else ''} | Gas: ₹{_amt(gas)}{' (est.)' if gas_from_est else ''} | Maint: ₹{_amt(maint)} | Other: ₹{_amt(common)}",
            f"Month total: ₹{_amt(month_total)} | Prev balance: ₹{_amt(prev_baki)}",
            f"Gross due: ₹{_amt(gross_baki)}",
            f"Paid: ₹{_amt(jama)}",
            f"{_bm_label} baki: ₹{_amt(outstanding)}" if outstanding >= 0 else f"Excess paid: ₹{_amt(-outstanding)}",
            "",
        ]
        # Cash receipts so far (part-settlement): one line per cash movement so
        # the tenant can reconcile the new balance against what they already
        # paid. Accounting-only DEPOSIT_UTILIZED rows move no cash — excluded.
        # Legacy rows (payment_subtype NULL) are cash and stay counted.
        _receipts = [h for h in history
                     if h.get('payment_subtype') in ('SETTLEMENT_EXTRA', 'DEPOSIT_REFUNDED')
                     or ((h.get('payment_type') or '').upper() in ('SETTLEMENT', 'REFUND')
                         and not h.get('payment_subtype'))]
        if _receipts:
            _wa_lines.append(f"Received ({len(_receipts)}):")
            for _r in _receipts:
                try:
                    _rd = datetime.strptime(str(_r['payment_date'])[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
                except Exception:
                    _rd = str(_r['payment_date'])[:10]
                _rkind = 'settlement' if (_r.get('payment_type') or '').upper() == 'SETTLEMENT' else 'refund'
                _wa_lines.append(f"{_rd}: ₹{_amt(_r['amount'])} via {(_r.get('payment_mode') or 'CASH')} — {_rkind}")
            if is_receivable:
                _wa_lines.append(f"Total received: ₹{_amt(total_settled)} | Still due: ₹{_amt(remaining_receivable)}")
            if is_payable:
                _wa_lines.append(f"Total refunded: ₹{_amt(total_refunded)} | Still refundable: ₹{_amt(remaining_payable)}")
            if not is_receivable and not is_payable:
                if total_settled > 0:
                    _wa_lines.append(f"Total received: ₹{_amt(total_settled)}")
                if total_refunded > 0:
                    _wa_lines.append(f"Total refunded: ₹{_amt(total_refunded)}")
            _wa_lines.append("")
        _wa_lines.extend([
            f"Security deposit held: ₹{_amt(deposit_received)}",
        ])
        _dash = "-" * 24
        if receivable_from_tenant > 0 and deposit_received > 0:
            # Dues partly offset by deposit: show the settlement arithmetic.
            _wa_lines.extend([
                f"Settled from deposit: ₹{_amt(deposit_used)}",
                f"{_amt(outstanding)} ({_bm_label} baki)",
                f"- {_amt(deposit_used)} (deposit)",
                _dash,
                f"*= {_amt(receivable_from_tenant)} Payable by {_tname} (Baki final)*",
            ])
        elif outstanding > 0 and deposit_received > 0:
            # Deposit covered the dues in full (receivable is 0).
            _wa_lines.extend([
                f"Settled from deposit: ₹{_amt(deposit_used)}",
                f"{_amt(outstanding)} ({_bm_label} baki)",
                f"- {_amt(deposit_used)} (deposit)",
                _dash,
                "*Dues covered by deposit*",
            ])
            if deposit_refund > 0:
                _wa_lines.append(f"= {_amt(deposit_refund)} Refundable to {_tname} (final)")
        if payable_to_tenant > 0 and outstanding <= 0:
            # Overpaid/credit: excess plus deposit comes back to the tenant.
            _wa_lines.extend([
                f"Excess paid: ₹{_amt(-outstanding)}",
                _dash,
                f"*= {_amt(payable_to_tenant)} Refundable to {_tname} (final)*",
            ])
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
            _wa_link = wm.wa_link(tenant.get('phone_number'), _wa_message, tenant.get('country_code') or '91')
        except Exception:
            _wa_link = None

        # Nested shape required by the settlement modal (tenant/ledger/settlement/deposit/history)
        return {
            "tenant": {
                "id": tenant_id,
                "tenant_name": tenant['tenant_name'],
                "display_address": tenant['display_address'],
                "phone_number": tenant['phone_number'],
                "country_code": tenant.get('country_code') or '91',
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
                "histmax_light": s['histmax_light'],
                "histmax_gas": s['histmax_gas'],
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
            "estimate_trueup": estimate_trueup,
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
            # Recompute status from stored jama vs new generated month_total.
            # Paid only when the whole month is clear (carry included); dues
            # remaining from carry still show Partial.
            cur.execute(f"""
                UPDATE rental_core.monthly_ledgers
                SET payment_status = CASE
                        {_paid_status_case()}
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
        used_estimates = s['used_estimates']
        est_light_snap = float(s['light']) if s['light_from_est'] else None
        est_gas_snap = float(s['gas']) if s['gas_from_est'] else None

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

        # Insert settlement payment(s) with proper sub-types for deposit accounting
        cur.execute("SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_batch FROM rental_core.tenant_payments")
        batch_id = cur.fetchone()['next_batch']

        payment_ids = []
        note = data.note or f'Final settlement {data.lease_end_date}'

        if payment_type == 'SETTLEMENT':
            # For SETTLEMENT: tenant pays cash (SETTLEMENT_EXTRA) + deposit utilized (DEPOSIT_UTILIZED)
            # deposit_used from settlement math
            deposit_used = float(s['deposit_used'])
            extra_cash = float(data.amount)  # cash from tenant = settlement amount

            # 1. DEPOSIT_UTILIZED (accounting entry, no cash moves now — the
            # deposit was received earlier at lease start, so mode is
            # ADJUSTMENT, never the cash mode the tenant just paid in).
            # Logged only once per closing month — later partial receipts just
            # add cash rows (same guard as the REFUND path below).
            _settle_util_logged = False
            if deposit_used > 0.01:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM rental_core.tenant_payments
                    WHERE tenant_id = %s AND billing_month = %s
                      AND payment_subtype = 'DEPOSIT_UTILIZED'
                """, (tenant_id, billing_month))
                if not cur.fetchone()['n']:
                    cur.execute("""
                        INSERT INTO rental_core.tenant_payments
                        (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id)
                        VALUES (%s, %s, %s, %s, %s, 'ADJUSTMENT', 'SETTLEMENT', 'DEPOSIT_UTILIZED', %s, %s) RETURNING id;
                    """, (
                        tenant_id,
                        tenant['room_id'],
                        billing_month,
                        payment_date,
                        deposit_used,
                        f'Deposit utilized for settlement {note}',
                        batch_id
                    ))
                    payment_ids.append(cur.fetchone()['id'])
                    _settle_util_logged = True

            # 2. SETTLEMENT_EXTRA (cash from tenant)
            if extra_cash > 0.01:
                cur.execute("""
                    INSERT INTO rental_core.tenant_payments
                    (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id)
                    VALUES (%s, %s, %s, %s, %s, %s, 'SETTLEMENT', 'SETTLEMENT_EXTRA', %s, %s) RETURNING id;
                """, (
                    tenant_id,
                    tenant['room_id'],
                    billing_month,
                    payment_date,
                    extra_cash,
                    data.payment_mode,
                    note,
                    batch_id
                ))
                payment_ids.append(cur.fetchone()['id'])

            payment_id = payment_ids[-1] if payment_ids else None

        elif payment_type == 'REFUND':
            # For REFUND: the deposit portion covering the dues is logged as
            # DEPOSIT_UTILIZED (ADJUSTMENT, no cash moves now), the payout as
            # DEPOSIT_REFUNDED. Utilization is logged only once per closing
            # month — later partial refunds just add payout rows.
            _refund_used = float(s['deposit_used'])
            _refund_util_logged = False
            if _refund_used > 0.01:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM rental_core.tenant_payments
                    WHERE tenant_id = %s AND billing_month = %s
                      AND payment_subtype = 'DEPOSIT_UTILIZED'
                """, (tenant_id, billing_month))
                if not cur.fetchone()['n']:
                    cur.execute("""
                        INSERT INTO rental_core.tenant_payments
                        (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id)
                        VALUES (%s, %s, %s, %s, %s, 'ADJUSTMENT', 'REFUND', 'DEPOSIT_UTILIZED', %s, %s) RETURNING id;
                    """, (
                        tenant_id,
                        tenant['room_id'],
                        billing_month,
                        payment_date,
                        _refund_used,
                        f'Deposit utilized for settlement {note}',
                        batch_id
                    ))
                    payment_ids.append(cur.fetchone()['id'])
                    _refund_util_logged = True
            cur.execute("""
                INSERT INTO rental_core.tenant_payments
                (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'REFUND', 'DEPOSIT_REFUNDED', %s, %s) RETURNING id;
            """, (
                tenant_id,
                tenant['room_id'],
                billing_month,
                payment_date,
                data.amount,
                data.payment_mode,
                note,
                batch_id
            ))
            payment_id = cur.fetchone()['id']
            payment_ids.append(payment_id)

        # Update tenant deposit settlement tracking
        # Track sub-payments for deposit accounting
        sub_payments = []
        if payment_type == 'SETTLEMENT':
            deposit_used = float(s['deposit_used'])
            if _settle_util_logged:
                sub_payments.append({"subtype": "DEPOSIT_UTILIZED", "amount": deposit_used})
            if float(data.amount) > 0.01:
                sub_payments.append({"subtype": "SETTLEMENT_EXTRA", "amount": float(data.amount)})
        elif payment_type == 'REFUND':
            if _refund_util_logged:
                sub_payments.append({"subtype": "DEPOSIT_UTILIZED", "amount": _refund_used})
            sub_payments.append({"subtype": "DEPOSIT_REFUNDED", "amount": float(data.amount)})

        settlement_entry = {
            "payment_id": payment_id,  # last/primary id for backward compat
            "payment_ids": payment_ids,  # all ids in this batch
            "sub_payments": sub_payments,  # split with sub-types
            "lease_end_date": data.lease_end_date,
            "billing_month": billing_month,
            "payment_date": data.payment_date,
            "payment_type": payment_type,
            "amount": float(data.amount),
            "payment_mode": data.payment_mode,
            "note": data.note or f'Final settlement {data.lease_end_date}',
            "recorded_at": datetime.now().isoformat(),
            "used_estimates": used_estimates,
            "est_light": est_light_snap,
            "est_gas": est_gas_snap,
            # Full effective breakdown on which basis this settlement was
            # processed — replayed in Customer View for closing months whose
            # actual ledger was never prepared.
            "brk": {
                "bhadu": float(s['bhadu']),
                "light": float(s['light']),
                "gas": float(s['gas']),
                "maint": float(s['maint']),
                "common": float(s['common'])
            }
        }

        cur.execute("SELECT settlement_history FROM rental_core.tenants WHERE id = %s", (tenant_id,))
        hist_row = cur.fetchone()
        settlement_history = hist_row['settlement_history'] if hist_row and hist_row['settlement_history'] else []
        settlement_history.append(settlement_entry)

        # Update deposit_settled flags
        # deposit_settled_amount tracks: DEPOSIT_UTILIZED + DEPOSIT_REFUNDED (what reduced deposit balance)
        new_deposit_settled = False
        new_deposit_settled_amount = float(tenant.get('deposit_settled_amount', 0) or 0)
        new_deposit_settled_date = tenant.get('deposit_settled_date')

        if payment_type == 'SETTLEMENT':
            # Only DEPOSIT_UTILIZED reduces deposit balance — counted when the
            # row is actually logged (once per closing month, see guard above).
            if _settle_util_logged:
                deposit_used = float(s['deposit_used'])
                new_deposit_settled_amount += deposit_used
            if is_full_settlement:
                new_deposit_settled = True
                new_deposit_settled_date = payment_date
        elif payment_type == 'REFUND':
            # DEPOSIT_UTILIZED + DEPOSIT_REFUNDED reduce deposit balance
            # (matches the delete-path recompute, which sums both)
            if _refund_util_logged:
                new_deposit_settled_amount += _refund_used
            new_deposit_settled_amount += float(data.amount)
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

        # On FULL settlement, close the lease-end month in every ledger view:
        # stamp it 'Settled'. Amounts are untouched (jama/new_baki stay the
        # monthly truth) — only the display status changes. Partial settlements
        # leave the true dues visible, so no stamp unless fully closed.
        if is_full_settlement:
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
                _cascade_ledger_chain(cur, tenant_id, billing_month)
            # settled_estimate marks settlements closed before the actual
            # utility bills arrived -> 'Settled (Est.)' badge + later true-up.
            cur.execute("UPDATE rental_core.monthly_ledgers SET payment_status = 'Settled', settled_estimate = %s WHERE tenant_id = %s AND billing_month = %s",
                        (used_estimates and is_full_settlement, tenant_id, billing_month))
            # A full settlement closes the whole chain, not just the last month:
            # intermediate months still showing Unpaid/Partial were covered by
            # this closing payment (their dues rolled into the settled gross).
            # Stamp them too so no dues total counts them again. Already-Paid
            # months keep their status; only the closing month carries the
            # estimate flag.
            cur.execute("""
                UPDATE rental_core.monthly_ledgers
                SET payment_status = 'Settled'
                WHERE tenant_id = %s AND billing_month <= %s
                  AND COALESCE(payment_status, 'Unpaid') NOT IN ('Paid', 'Settled')
            """, (tenant_id, billing_month))

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

def _split_phone(cc, raw):
    """Split a dialled number into (country_code, local_digits).

    Accepts a pasted full international number (strips a leading 91/977)
    or plain local digits. Both India and Nepal use 10-digit mobiles.
    """
    import re as _re
    cc = str(cc or "91").strip().lstrip("+")
    if cc not in ("91", "977"):
        raise HTTPException(status_code=400, detail="country_code must be 91 (India) or 977 (Nepal)")
    digits = _re.sub(r"\D", "", raw or "")
    while digits.startswith("0") and len(digits) > 10:
        digits = digits[1:]
    for _code in ("977", "91"):
        if digits.startswith(_code) and len(digits) > 10:
            digits = digits[len(_code):]
            break
    if len(digits) != 10:
        raise HTTPException(status_code=400, detail="Phone number must have 10 digits")
    return cc, digits


@app.post("/api/tenants")
def create_tenant(tenant: TenantCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        # Empty strings must become NULL for DATE columns
        for _f in ("pending_deposit_due_date", "lease_start_date", "lease_end_date"):
            if getattr(tenant, _f) == "":
                setattr(tenant, _f, None)
        _cc, _digits = _split_phone(tenant.country_code, tenant.phone_number)
        # Occupancy guard: one active stay per room (sharing uses one row).
        cur.execute("""
            SELECT id, tenant_name FROM rental_core.tenants
            WHERE room_id = %s AND is_active = 'Y' LIMIT 1
        """, (tenant.room_id,))
        _occ = cur.fetchone()
        if _occ:
            raise HTTPException(status_code=400, detail=f"Room already occupied by active tenant #{_occ['id']} ({_occ['tenant_name']}). End their stay first.")
        # Optional link to the same person's previous stay (navigational only).
        _prev = tenant.previous_tenant_id
        if _prev is not None:
            cur.execute("SELECT id FROM rental_core.tenants WHERE id = %s", (_prev,))
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail=f"previous_tenant_id {_prev} not found")
        cur.execute("""
            INSERT INTO rental_core.tenants
            (room_id, tenant_name, phone_number, country_code, base_rent_applied, maintenance_applied, security_deposit_applied,
             booking_amount_paid, pending_deposit_due_date, lease_start_date, lease_end_date, is_active, previous_tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Y', %s) RETURNING id;
        """, (
            tenant.room_id, tenant.tenant_name, _digits, _cc,
            tenant.base_rent_applied, tenant.maintenance_applied, tenant.security_deposit_applied,
            tenant.booking_amount_paid, tenant.pending_deposit_due_date,
            tenant.lease_start_date, tenant.lease_end_date, _prev
        ))
        new_id = cur.fetchone()['id']
        
        # If booking amount was paid, create a DEPOSIT payment record
        if tenant.booking_amount_paid and tenant.booking_amount_paid > 0:
            cur.execute("""
                INSERT INTO rental_core.tenant_payments
                (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'DEPOSIT', 'DEPOSIT_RECEIVED', %s, %s);
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


@app.get("/api/tenants/release-context")
def tenant_release_context(phone: str = Query(..., min_length=4, max_length=20)):
    """Prior stays for a phone number (re-lease assist, read-only).

    Returns each past stay with its unit, lease dates, active flag and open
    dues (latest new_baki). Never blocks or merges anything.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, t.tenant_name, t.phone_number, t.country_code, t.room_id, t.is_active,
               t.lease_start_date, t.lease_end_date, t.deposit_settled,
               r.display_address, pm.apartment_name,
               COALESCE((
                   SELECT ml.new_baki FROM rental_core.monthly_ledgers ml
                   WHERE ml.tenant_id = t.id
                   ORDER BY ml.billing_month DESC LIMIT 1
               ), 0.00) AS open_dues
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        LEFT JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE t.phone_number = %s
        ORDER BY t.lease_start_date DESC NULLS LAST, t.id DESC
    """, (phone,))
    stays = [
        {
            "tenant_id": r['id'],
            "tenant_name": r['tenant_name'],
            "country_code": r.get('country_code') or '91',
            "room_id": r['room_id'],
            "display_address": r['display_address'],
            "apartment_name": r['apartment_name'],
            "is_active": r['is_active'],
            "lease_start_date": str(r['lease_start_date']) if r['lease_start_date'] else None,
            "lease_end_date": str(r['lease_end_date']) if r['lease_end_date'] else None,
            "deposit_settled": bool(r['deposit_settled']),
            "open_dues": float(r['open_dues'] or 0),
        } for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return {"phone": phone, "stays": stays}


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
            ml.settled_estimate,
            ml.light_bill_type,
            ml.month_total,
            ml.gross_baki,
            ml.light_bill_actual,
            ml.light_bill_manual,
            ml.gas_bill_actual,
            ml.gas_bill_manual,
            ml.maintenance_actual,
            ml.maintenance_manual,
            ml.common_misc_actual,
            ml.common_misc_manual,
            ml.manual_override,
            r.display_address,
            pm.id AS property_id,
            pm.apartment_name,
            t.tenant_name,
            t.phone_number,
            t.country_code
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


def _paid_status_case(jama_expr="jama", prev_baki_expr="prev_baki", prev_jama_expr="prev_jama"):
    """Carry-aware Paid branches shared by every payment_status CASE.

    Paid iff the carry-inclusive outstanding is fully covered:
        prev_baki - prev_jama + month_total - jama <= 0
    The scope guard keeps fully-empty months (no bills, no payments, no
    carry) Unpaid, while a month fully covered by carried-in credit alone
    (jama = 0) still reads Paid. The *_expr args are column names or %s
    placeholders — pass %s whenever the same UPDATE also rewrites that
    column, because SET expressions all see pre-update row values.
    Callers keep their own Settled guard + Partial/Unpaid fallbacks around
    this fragment.
    """
    return f"""WHEN ({prev_baki_expr} - {prev_jama_expr} + month_total - {jama_expr}) <= 0
                 AND (month_total > 0 OR {jama_expr} > 0 OR {prev_baki_expr} > 0 OR {prev_jama_expr} > 0)
                THEN 'Paid'"""


def _recompute_status(cur, tenant_id, billing_month):
    """Recompute one month's payment_status from stored jama vs generated
    month_total (same CASE as _sync_ledger_jama). Used to authoritatively drop
    a 'Settled' mark — e.g. when a settlement is deleted. Clears the estimate
    flag too (no longer settled at all). Never touches amounts."""
    cur.execute(f"""
        UPDATE rental_core.monthly_ledgers
        SET payment_status = CASE
                {_paid_status_case()}
                WHEN jama > 0 THEN 'Partial'
                ELSE 'Unpaid'
            END,
            settled_estimate = FALSE
        WHERE tenant_id = %s AND billing_month = %s
    """, (tenant_id, billing_month))


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

    cur.execute(f"""
        UPDATE rental_core.monthly_ledgers
        SET jama = %s,
            jama_date = %s,
            payment_status = CASE
                WHEN payment_status = 'Settled' THEN 'Settled'
                {_paid_status_case("%s")}
                WHEN %s > 0 THEN 'Partial'
                ELSE 'Unpaid'
            END
        WHERE tenant_id = %s AND billing_month = %s
    """, (total_paid, row['last_pay_date'], total_paid, total_paid, total_paid, tenant_id, billing_month))

    if total_paid <= 0:
        # If no payments remain and the ledger was only a placeholder created by
        # an advance payment (no bills compiled yet), remove it.
        # Never remove a 'Settled' closing month — the mark is settlement truth.
        cur.execute("""
            DELETE FROM rental_core.monthly_ledgers
            WHERE tenant_id = %s AND billing_month = %s
              AND bhadu_applied = 0 AND light_bill = 0 AND gas_bill = 0
              AND maintenance_applied = 0 AND common_misc_expenses = 0
              AND jama = 0
              AND payment_status IS DISTINCT FROM 'Settled';
        """, (tenant_id, billing_month))

    _cascade_ledger_chain(cur, tenant_id, billing_month)


def _cascade_ledger_chain(cur, tenant_id, from_month):
    """Recompute prev_baki/prev_jama for every month after from_month so a
    payment recorded against an older month flows through the whole ledger
    chain (new_baki is generated, but prev_baki/prev_jama are stored).
    Each touched month's payment_status is recomputed with the shared
    carry-aware rule too (Settled months are never altered) so a changed
    carry cannot leave a stale Paid/Partial behind."""
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
        # NOTE: prev_baki/prev_jama are passed as %s into the CASE (not column
        # refs) because this same UPDATE rewrites them — SET expressions all
        # see pre-update row values.
        cur.execute(f"""
            UPDATE rental_core.monthly_ledgers
            SET prev_baki = %s, prev_jama = %s,
                payment_status = CASE
                    WHEN payment_status = 'Settled' THEN 'Settled'
                    {_paid_status_case("jama", "%s", "%s")}
                    WHEN jama > 0 THEN 'Partial'
                    ELSE 'Unpaid'
                END
            WHERE tenant_id = %s AND billing_month = %s
            RETURNING COALESCE(new_baki, 0.00) AS new_baki
        """, (next_prev_baki, next_prev_jama,
              next_prev_baki, next_prev_jama, next_prev_baki, next_prev_jama,
              tenant_id, bm))
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
               p.payment_mode, p.payment_type, p.payment_subtype, p.note, p.created_at, p.batch_id,
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


@app.put("/api/payments/{payment_id}")
def update_payment(payment_id: int, data: PaymentUpdate):
    """Edit one payment row (split batches: only this row, never the batch).

    - RENT/DEPOSIT rows: amount, date, mode, note, billing month and RENT<->DEPOSIT
      type changes allowed; the old and new months are re-synced (jama/status
      recomputed, chain cascaded).
    - SETTLEMENT/REFUND rows: only date, mode and note (bookkeeping amounts,
      months and types are settlement truth — delete + re-record to change
      those). Matching settlement-history snapshots are updated too.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM rental_core.tenant_payments WHERE id = %s", (payment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Payment not found")

        old_type = row['payment_type']
        old_bm = row['billing_month']
        is_settlement = old_type in ('SETTLEMENT', 'REFUND')

        new_date = row['payment_date']
        if data.payment_date is not None:
            try:
                new_date = datetime.strptime(data.payment_date[:10], "%Y-%m-%d").date()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid payment_date, use YYYY-MM-DD")

        new_amount = float(row['amount'])
        if data.amount is not None:
            if is_settlement:
                raise HTTPException(status_code=400, detail="Settlement amounts are locked — delete and re-record instead")
            new_amount = float(data.amount)

        new_mode = data.payment_mode if data.payment_mode is not None else row['payment_mode']
        new_type = data.payment_type if data.payment_type is not None else old_type
        if new_type != old_type:
            if is_settlement or new_type in ('SETTLEMENT', 'REFUND'):
                raise HTTPException(status_code=400, detail="Payment type cannot be changed to/from SETTLEMENT/REFUND")
            if {old_type, new_type} - {'RENT', 'DEPOSIT'}:
                raise HTTPException(status_code=400, detail="Payment type can only switch between RENT and DEPOSIT")

        new_bm = data.billing_month if data.billing_month is not None else old_bm
        if is_settlement and new_bm != old_bm:
            raise HTTPException(status_code=400, detail="Settlement billing month is locked — delete and re-record instead")

        new_note = data.note if data.note is not None else row['note']

        cur.execute("""
            UPDATE rental_core.tenant_payments
            SET payment_date = %s, amount = %s, payment_mode = %s,
                payment_type = %s, billing_month = %s, note = %s
            WHERE id = %s
        """, (new_date, new_amount, new_mode, new_type, new_bm, new_note, payment_id))

        # Re-sync ledgers for old and new months when rent-side data moved.
        for _bm in {old_bm, new_bm}:
            if not _bm:
                continue
            if old_type == 'RENT' or new_type == 'RENT':
                _sync_ledger_jama(cur, row['tenant_id'], _bm)

        # Keep settlement-history snapshots truthful for cosmetic edits.
        if is_settlement and (data.payment_date is not None or data.payment_mode is not None or data.note is not None):
            cur.execute("SELECT settlement_history FROM rental_core.tenants WHERE id = %s", (row['tenant_id'],))
            _hrow = cur.fetchone()
            _hist = _hrow['settlement_history'] if _hrow and _hrow['settlement_history'] else []
            _changed = False
            for _h in _hist:
                if not isinstance(_h, dict):
                    continue
                _ids = set(_h.get('payment_ids') or [])
                if _h.get('payment_id') is not None:
                    _ids.add(_h.get('payment_id'))
                if payment_id in _ids:
                    if data.payment_date is not None:
                        _h['payment_date'] = str(new_date)
                    if data.payment_mode is not None:
                        _h['payment_mode'] = new_mode
                    if data.note is not None:
                        _h['note'] = new_note
                    _changed = True
            if _changed:
                cur.execute("UPDATE rental_core.tenants SET settlement_history = %s WHERE id = %s",
                            (json.dumps(_hist), row['tenant_id']))

        conn.commit()
        cur.execute("SELECT * FROM rental_core.tenant_payments WHERE id = %s", (payment_id,))
        _u = cur.fetchone()
        return {
            "status": "SUCCESS",
            "payment": {
                "id": _u['id'], "tenant_id": _u['tenant_id'],
                "billing_month": _u['billing_month'],
                "payment_date": str(_u['payment_date']), "amount": float(_u['amount']),
                "payment_mode": _u['payment_mode'], "payment_type": _u['payment_type'],
                "note": _u['note'],
            },
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


def _settlement_month_figures(cur, tenant_id, bm):
    """Settlement math for one closing month, shared by accept-notify and the
    yearly ledger. Resolves lease-end from the latest settlement-history entry
    for the month, else the tenant's lease_end_date when it falls in the same
    month, else month-end (plain full-month close). Returns (le_date, s)."""
    le_str = None
    try:
        cur.execute("SELECT settlement_history, lease_end_date FROM rental_core.tenants WHERE id = %s", (tenant_id,))
        _trow = cur.fetchone()
        _hist = (_trow['settlement_history'] if _trow and _trow['settlement_history'] else []) or []
        _cands = [e for e in _hist if isinstance(e, dict) and e.get('billing_month') == bm and e.get('lease_end_date')]
        if _cands:
            _cands.sort(key=lambda e: str(e.get('recorded_at') or ''), reverse=True)
            le_str = str(_cands[0]['lease_end_date'])[:10]
        if not le_str and _trow and _trow['lease_end_date'] and str(_trow['lease_end_date'])[:7] == bm:
            le_str = str(_trow['lease_end_date'])[:10]
    except Exception:
        le_str = None
    if not le_str:
        import calendar as _cal
        _y, _m = map(int, bm.split('-'))
        le_str = f"{bm}-{_cal.monthrange(_y, _m)[1]:02d}"
    le_date = datetime.strptime(le_str[:10], "%Y-%m-%d").date()
    s = _compute_settlement(cur, tenant_id, le_date, bm,
                            {'bhadu': None, 'light': None, 'gas': None,
                             'maintenance': None, 'common': None})
    return le_date, s


def _settlement_accept_message(cur, pay, ptype):
    """Build the instant-accept message for one cash SETTLEMENT/REFUND row.

    Figures come from settlement math computed AFTER the payment was
    recorded: base = dues immediately before this receipt (receivable) or
    cash refunded before it (refund); remaining = dues/refund-baki now.
    """
    import reminder_message as rm
    tid = pay['tenant_id']
    bm = pay['billing_month']
    amt = float(pay['amount'] or 0)
    _, s = _settlement_month_figures(cur, tid, bm)
    if ptype == 'SETTLEMENT':
        remaining = max(0.0, float(s['remaining_receivable']))
        return rm.settlement_accept_payload(cur, pay, bm, remaining + amt, remaining, is_refund=False)
    remaining = max(0.0, float(s['remaining_payable']))
    before = max(0.0, float(s['total_refunded']) - amt)
    return rm.settlement_accept_payload(cur, pay, bm, before, remaining, is_refund=True)


@app.post("/api/payments/{payment_id}/accept-notify")
def payment_accept_notify(payment_id: int):
    """Queue a payment-accepted WhatsApp (no due date) for one payment.

    RENT/DEPOSIT: existing monthly flows. SETTLEMENT/REFUND: cash rows only
    (SETTLEMENT_EXTRA / DEPOSIT_REFUNDED, incl. legacy NULL subtype) —
    accounting-only ADJUSTMENT/DEPOSIT_UTILIZED rows move no cash and are
    rejected. Idempotent per payment: re-calling returns the existing
    queued/sent row.
    """
    import reminder_message as rm
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM rental_core.tenant_payments WHERE id = %s", (payment_id,))
        pay = cur.fetchone()
        if not pay:
            raise HTTPException(status_code=404, detail="Payment not found")
        ptype = (pay['payment_type'] or 'RENT').upper()
        subtype = (pay.get('payment_subtype') or '').upper() or None
        if ptype in ('SETTLEMENT', 'REFUND'):
            _allowed = {'SETTLEMENT': ('SETTLEMENT_EXTRA', None),
                        'REFUND': ('DEPOSIT_REFUNDED', None)}[ptype]
            if subtype not in _allowed or (pay.get('payment_mode') or '').upper() == 'ADJUSTMENT':
                raise HTTPException(status_code=400, detail="Only cash settlement/refund receipts can notify (no message for adjustment entries)")
        elif ptype not in ('RENT', 'DEPOSIT'):
            raise HTTPException(status_code=400, detail="Payment-accepted messages support RENT, DEPOSIT, SETTLEMENT and REFUND only")
        cur.execute("""
            SELECT id, message FROM rental_core.reminder_send_log
            WHERE payment_id = %s AND send_type = 'NOTIFY' AND status IN ('QUEUED', 'SENT')
            ORDER BY id DESC LIMIT 1
        """, (payment_id,))
        existing = cur.fetchone()
        if ptype in ('SETTLEMENT', 'REFUND'):
            built = _settlement_accept_message(cur, pay, ptype)
        else:
            built = rm.accept_payload(cur, pay, current_billing_period())
        if existing:
            return {"status": "SUCCESS", "duplicate": True,
                    "message": existing['message'], "wa_link": built["wa_link"]}
        cur.execute("""
            INSERT INTO rental_core.reminder_send_log
            (batch_id, reminder_id, tenant_id, phone_number, send_type,
             message, status, payment_id)
            VALUES (%s, NULL, %s, %s, 'NOTIFY', %s, 'QUEUED', %s)
        """, (datetime.now().strftime("%Y%m%d%H%M%S") + "_accept",
              pay['tenant_id'], built.get('phone_number') or '', built["message"], payment_id))
        conn.commit()
        return {"status": "SUCCESS", "duplicate": False,
                "message": built["message"], "wa_link": built["wa_link"]}
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

        # Cancel queued (unsent) notifications for the deleted payments so no
        # phantom message goes out for a payment that no longer exists.
        cur.execute("""
            UPDATE rental_core.reminder_send_log
               SET status = 'CANCELLED', error = 'payment deleted'
             WHERE payment_id = ANY(%s) AND status = 'QUEUED'
        """, (deleted_ids,))

        # If a SETTLEMENT/REFUND payment was deleted, reopen the settlement bookkeeping:
        # drop its history entries, recompute settled flags, reactivate tenant if fully reopened.
        if row['payment_type'] in ('SETTLEMENT', 'REFUND'):
            tenant_id = row['tenant_id']
            cur.execute("SELECT settlement_history FROM rental_core.tenants WHERE id = %s", (tenant_id,))
            hist_row = cur.fetchone()
            history = hist_row['settlement_history'] if hist_row and hist_row['settlement_history'] else []
            history = [h for h in history if h.get('payment_id') not in deleted_ids]
            # Recompute remaining by sub-type for accurate deposit tracking.
            # Cash legs only for the reopen flag (legacy NULL-subtype rows are
            # cash); utilized rows always delete with their batch.
            cur.execute("""
                SELECT
                    COALESCE(SUM(amount) FILTER (WHERE payment_subtype = 'DEPOSIT_UTILIZED'), 0) AS utilized,
                    COALESCE(SUM(amount) FILTER (WHERE payment_subtype = 'SETTLEMENT_EXTRA'
                        OR (payment_type = 'SETTLEMENT' AND payment_subtype IS NULL)), 0) AS extra,
                    COALESCE(SUM(amount) FILTER (WHERE payment_subtype = 'DEPOSIT_REFUNDED'
                        OR (payment_type = 'REFUND' AND payment_subtype IS NULL)), 0) AS refunded
                FROM rental_core.tenant_payments WHERE tenant_id = %s
            """, (tenant_id,))
            rem = cur.fetchone()
            remaining_utilized = float(rem['utilized'] or 0)
            remaining_extra = float(rem['extra'] or 0)
            remaining_refunded = float(rem['refunded'] or 0)
            fully_reopened = (remaining_extra <= 0.01 and remaining_refunded <= 0.01)
            # deposit_settled_amount tracks UTILIZED + REFUNDED (what reduced deposit balance)
            new_deposit_settled_amount = remaining_utilized + remaining_refunded
            cur.execute("""
                UPDATE rental_core.tenants
                SET settlement_history = %s,
                    deposit_settled = CASE WHEN %s THEN FALSE ELSE deposit_settled END,
                    deposit_settled_amount = %s,
                    deposit_settled_date = CASE WHEN %s THEN NULL ELSE deposit_settled_date END,
                    is_active = CASE WHEN %s THEN 'Y' ELSE is_active END
                WHERE id = %s
            """, (json.dumps(history), fully_reopened, new_deposit_settled_amount, fully_reopened, fully_reopened, tenant_id))
            # Undo the chain stamp: every month this settlement had closed shows
            # true dues again. (The _sync calls above now preserve 'Settled',
            # so this explicit recompute is the authoritative revert.)
            _max_bm = max(b['billing_month'] for b in batch_rows)
            cur.execute("""
                SELECT billing_month FROM rental_core.monthly_ledgers
                WHERE tenant_id = %s AND payment_status = 'Settled'
                  AND billing_month <= %s
            """, (tenant_id, _max_bm))
            for _r in cur.fetchall():
                _recompute_status(cur, tenant_id, _r['billing_month'])
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


@app.get("/api/torrent-reports/status")
def get_torrent_reports_status():
    """Months with torrent bills in DB (+ archive files if present)."""
    import os
    from datetime import datetime
    base_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(base_dir, "torrent_power_report.csv")
    xlsx_path = os.path.join(base_dir, "torrent_power_report.xlsx")
    csv_exists = os.path.exists(csv_path)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT execution_month, COUNT(*) AS n
        FROM rental_core.torrent_bill_master
        GROUP BY execution_month ORDER BY execution_month DESC;
    """)
    months = []
    for r in cur.fetchall():
        bm = r['execution_month']
        try:
            label = datetime.strptime(bm, "%Y-%m").strftime("%b %Y")
        except Exception:
            label = bm
        months.append({
            "month": bm, "tag": bm, "label": label,
            "hasDb": True, "dbCount": int(r['n']), "csvExists": csv_exists,
        })
    cur.close()
    conn.close()
    last_updated = None
    if csv_exists:
        last_updated = datetime.fromtimestamp(os.path.getmtime(csv_path)).isoformat()
    return {
        "csvExists": csv_exists,
        "lastUpdated": last_updated,
        "months": months,
        "currentMonth": months[0]['month'] if months else None,
    }


@app.get("/api/torrent-reports/download")
def download_torrent_report(type: str = "csv", month: str = None):
    """Tenant + service wise torrent electricity report, generated from DB.

    Effective bill mirrors month compile: NOTICE bills use
    (current - previous) floored at 0, else amount with last_bill_amt
    fallback (covers scrape-gap months where amount is 0).
    """
    import io
    import csv
    from fastapi.responses import StreamingResponse
    from fastapi import HTTPException
    if month is not None and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month, use YYYY-MM")
    conn = get_db()
    cur = conn.cursor()
    if not month:
        cur.execute("SELECT MAX(execution_month) AS m FROM rental_core.torrent_bill_master")
        row = cur.fetchone()
        month = row['m'] if row and row['m'] else None
    if not month:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="No torrent bills found in database.")
    # Chained effective bills per service: a NOTICE month's usage is its
    # bill minus the PREVIOUS EFFECTIVE bill (not the previous raw amount),
    # walking back through that service's history.
    cur.execute("""
        SELECT t.tenant_name, lt.latest_name, r.display_address, pm.apartment_name,
               tbm.service_number, tbm.execution_month, tbm.bill_type,
               COALESCE(tbm.amount, 0) AS amount,
               tbm.last_bill_amt, tbm.last_pay_date, tbm.last_bill_date,
               tbm.last_billed_units
        FROM rental_core.torrent_bill_master tbm
        LEFT JOIN rental_core.utility_metrics um ON um.service_number = tbm.service_number
        LEFT JOIN rental_core.rooms r ON r.id = um.room_id
        LEFT JOIN rental_core.property_masters pm ON pm.id = r.master_id
        LEFT JOIN rental_core.tenants t ON t.room_id = r.id AND t.is_active = 'Y'
        LEFT JOIN LATERAL (
            SELECT lt.tenant_name AS latest_name
            FROM rental_core.tenants lt
            WHERE lt.room_id = r.id
            ORDER BY lt.id DESC LIMIT 1
        ) lt ON true
        WHERE tbm.service_number IN (
            SELECT DISTINCT service_number FROM rental_core.torrent_bill_master
            WHERE execution_month = %s
        ) AND tbm.execution_month <= %s
        ORDER BY tbm.service_number, tbm.execution_month;
    """, (month, month))
    chain_rows = cur.fetchall()
    cur.close()
    conn.close()
    if not [r for r in chain_rows if r['execution_month'] == month]:
        raise HTTPException(status_code=404, detail=f"No torrent bills found for {month}.")
    out_rows = []
    _prev_eff = {}
    for r in chain_rows:
        amt = float(r['amount'] or 0)
        last_amt = float(r['last_bill_amt'] or 0)
        base = amt if amt else last_amt
        if (r['bill_type'] or '') == 'NOTICE' and r['service_number'] in _prev_eff:
            effective = max(base - _prev_eff[r['service_number']], 0.0)
        else:
            effective = base
        _prev_eff[r['service_number']] = effective
        if r['execution_month'] != month:
            continue
        out_rows.append({
            "Tenant": r['tenant_name'] or r['latest_name'] or "—",
            "Address": r['display_address'] or "—",
            "Apartment": r['apartment_name'] or "—",
            "Service Number": r['service_number'],
            "Bill Type": r['bill_type'] or "—",
            "Amount": round(amt, 2),
            "Last Bill Amt": round(last_amt, 2),
            "Effective Bill": round(effective, 2),
            "Last Pay Date": str(r['last_pay_date']) if r['last_pay_date'] else "",
            "Last Bill Date": str(r['last_bill_date']) if r['last_bill_date'] else "",
            "Billed Units": float(r['last_billed_units']) if r['last_billed_units'] is not None else "",
        })
    headers = list(out_rows[0].keys())
    if type == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=headers)
        w.writeheader()
        w.writerows(out_rows)
        data = io.BytesIO(buf.getvalue().encode())
        return StreamingResponse(data, media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=torrent_report_{month}.csv"})
    elif type == "xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"Torrent {month}"
        ws.append(headers)
        for row in out_rows:
            ws.append([row[h] for h in headers])
        data = io.BytesIO()
        wb.save(data)
        data.seek(0)
        return StreamingResponse(data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=torrent_report_{month}.xlsx"})
    raise HTTPException(status_code=400, detail="Invalid type. Use 'csv' or 'xlsx'")


@app.get("/api/reports/deposit-summary/download")
def download_deposit_summary_report(type: str = "csv", month: str = None):
    """Generate and download deposit summary report."""
    import os
    import csv
    import io
    from fastapi.responses import StreamingResponse
    from fastapi import HTTPException
    
    conn = get_db()
    cur = conn.cursor()
    
    where = ["p.payment_type IN ('DEPOSIT', 'SETTLEMENT', 'REFUND')", "p.payment_subtype IS NOT NULL"]
    params = []
    if month:
        where.append("p.payment_date >= %s AND p.payment_date < %s")
        params.append(month + "-01")
        # Calculate next month
        from datetime import datetime
        dt = datetime.strptime(month, "%Y-%m")
        if dt.month == 12:
            next_month = f"{dt.year + 1}-01-01"
        else:
            next_month = f"{dt.year}-{dt.month + 1:02d}-01"
        params.append(next_month)
    where_clause = " AND ".join(where)
    
    cur.execute(f"""
        SELECT
            pm.id AS property_id,
            pm.apartment_name,
            pm.plot_number,
            t.id AS tenant_id,
            t.tenant_name,
            t.room_id,
            t.security_deposit_applied AS required_deposit,
            r.display_address,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_RECEIVED' THEN p.amount ELSE 0 END), 0) AS received,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_UTILIZED' THEN p.amount ELSE 0 END), 0) AS utilized,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'DEPOSIT_REFUNDED' THEN p.amount ELSE 0 END), 0) AS refunded,
            COALESCE(SUM(CASE WHEN p.payment_subtype = 'SETTLEMENT_EXTRA' THEN p.amount ELSE 0 END), 0) AS extra_settlement
        FROM rental_core.tenants t
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        LEFT JOIN rental_core.tenant_payments p
            ON p.tenant_id = t.id
            AND p.payment_type IN ('DEPOSIT', 'SETTLEMENT', 'REFUND')
            AND p.payment_subtype IS NOT NULL
            {'AND ' + where_clause if where_clause else ''}
        GROUP BY pm.id, pm.apartment_name, pm.plot_number, t.id, t.tenant_name, t.room_id, t.security_deposit_applied, r.display_address
        ORDER BY pm.apartment_name, r.display_address;
    """, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    # Aggregate by property
    by_property = {}
    for r in rows:
        pid = r['property_id']
        if pid not in by_property:
            by_property[pid] = {
                "property_id": pid, "apartment_name": r['apartment_name'], "plot_number": r['plot_number'],
                "total_required": 0.0, "total_received": 0.0, "total_utilized": 0.0,
                "total_refunded": 0.0, "total_extra": 0.0, "tenants": []
            }
        prop = by_property[pid]
        required = float(r['required_deposit'] or 0)
        received = float(r['received'] or 0)
        utilized = float(r['utilized'] or 0)
        refunded = float(r['refunded'] or 0)
        extra = float(r['extra_settlement'] or 0)
        balance = max(0.0, received - utilized - refunded)
        
        prop["total_required"] += required
        prop["total_received"] += received
        prop["total_utilized"] += utilized
        prop["total_refunded"] += refunded
        prop["total_extra"] += extra
        prop["tenants"].append({
            "tenant_id": r['tenant_id'], "tenant_name": r['tenant_name'],
            "room_id": r['room_id'], "display_address": r['display_address'],
            "required": required, "received": received, "utilized": utilized,
            "refunded": refunded, "extra": extra, "balance": balance,
        })
    
    grand = {
        "total_required": sum(p["total_required"] for p in by_property.values()),
        "total_received": sum(p["total_received"] for p in by_property.values()),
        "total_utilized": sum(p["total_utilized"] for p in by_property.values()),
        "total_refunded": sum(p["total_refunded"] for p in by_property.values()),
        "total_extra": sum(p["total_extra"] for p in by_property.values()),
    }
    grand["total_balance"] = max(0.0, grand["total_received"] - grand["total_utilized"] - grand["total_refunded"])
    
    if type == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Property", "Plot", "Required", "Received", "Utilized", "Refunded", "Extra Settlement", "Balance"])
        for p in by_property.values():
            balance = p["total_received"] - p["total_utilized"] - p["total_refunded"]
            writer.writerow([p["apartment_name"], p["plot_number"], p["total_required"], p["total_received"], p["total_utilized"], p["total_refunded"], p["total_extra"], max(0.0, balance)])
        writer.writerow(["GRAND TOTAL", "", grand["total_required"], grand["total_received"], grand["total_utilized"], grand["total_refunded"], grand["total_extra"], max(0.0, grand["total_balance"])])
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=deposit_summary_{month or 'all'}.csv"}
        )
    else:
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Deposit Summary"
            ws.append(["Property", "Plot", "Required", "Received", "Utilized", "Refunded", "Extra Settlement", "Balance"])
            for p in by_property.values():
                balance = p["total_received"] - p["total_utilized"] - p["total_refunded"]
                ws.append([p["apartment_name"], p["plot_number"], p["total_required"], p["total_received"], p["total_utilized"], p["total_refunded"], p["total_extra"], max(0.0, balance)])
            ws.append(["GRAND TOTAL", "", grand["total_required"], grand["total_received"], grand["total_utilized"], grand["total_refunded"], grand["total_extra"], max(0.0, grand["total_balance"])])
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename=deposit_summary_{month or 'all'}.xlsx"}
            )
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed")


@app.get("/api/reports/settlements/download")
def download_settlements_report(
    type: str = "csv",
    from_date: str = None,
    to_date: str = None,
    property_id: int = None,
    payment_type: str = None
):
    """Generate and download settlements report."""
    import io
    import csv
    from fastapi.responses import StreamingResponse
    from fastapi import HTTPException
    
    conn = get_db()
    cur = conn.cursor()
    
    where = ["p.payment_type IN ('SETTLEMENT', 'REFUND')", "p.payment_subtype IS NOT NULL"]
    params = []
    if from_date:
        where.append("p.payment_date >= %s")
        params.append(from_date)
    if to_date:
        where.append("p.payment_date <= %s")
        params.append(to_date)
    if property_id:
        where.append("pm.id = %s")
        params.append(property_id)
    if payment_type:
        where.append("p.payment_type = %s")
        params.append(payment_type)
    where_clause = " AND ".join(where)
    
    cur.execute(f"""
        SELECT
            p.id, p.payment_date, p.payment_type, p.payment_subtype, p.amount,
            p.payment_mode, p.note, p.billing_month,
            t.id AS tenant_id, t.tenant_name,
            r.display_address, pm.id AS property_id, pm.apartment_name
        FROM rental_core.tenant_payments p
        JOIN rental_core.tenants t ON p.tenant_id = t.id
        JOIN rental_core.rooms r ON t.room_id = r.id
        JOIN rental_core.property_masters pm ON r.master_id = pm.id
        WHERE {where_clause}
        ORDER BY p.payment_date DESC, p.id DESC;
    """, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    if type == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Payment ID", "Date", "Type", "Sub-Type", "Amount", "Mode", "Note", "Billing Month", "Tenant", "Address", "Property"])
        for r in rows:
            writer.writerow([r['id'], r['payment_date'], r['payment_type'], r['payment_subtype'], r['amount'], r['payment_mode'], r['note'], r['billing_month'], r['tenant_name'], r['display_address'], r['apartment_name']])
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=settlements_report.csv"}
        )
    else:
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Settlements"
            ws.append(["Payment ID", "Date", "Type", "Sub-Type", "Amount", "Mode", "Note", "Billing Month", "Tenant", "Address", "Property"])
            for r in rows:
                ws.append([r['id'], r['payment_date'], r['payment_type'], r['payment_subtype'], r['amount'], r['payment_mode'], r['note'], r['billing_month'], r['tenant_name'], r['display_address'], r['apartment_name']])
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=settlements_report.xlsx"}
            )
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed")


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


@app.get("/api/tenants/{tenant_id}/deposit-ledger")
def get_tenant_deposit_ledger(tenant_id: int):
    """Per-tenant deposit ledger with running balance and sub-type breakdown.

    Returns all DEPOSIT_RECEIVED, DEPOSIT_UTILIZED, SETTLEMENT_EXTRA, DEPOSIT_REFUNDED
    transactions with running balance. Balance = Received - Utilized - Refunded.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        # Verify tenant exists
        cur.execute("SELECT id, tenant_name, room_id, security_deposit_applied FROM rental_core.tenants WHERE id = %s", (tenant_id,))
        tenant = cur.fetchone()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        # Get all relevant payment sub-types for this tenant, ordered by date
        cur.execute("""
            SELECT id, payment_date, amount, payment_mode, payment_type, payment_subtype, note, batch_id, billing_month
            FROM rental_core.tenant_payments
            WHERE tenant_id = %s
              AND payment_type IN ('DEPOSIT', 'SETTLEMENT', 'REFUND')
              AND payment_subtype IS NOT NULL
            ORDER BY payment_date, id
        """, (tenant_id,))
        rows = cur.fetchall()

        transactions = []
        balance = 0.0
        totals = {"received": 0.0, "utilized": 0.0, "extra": 0.0, "refunded": 0.0}

        for r in rows:
            subtype = r['payment_subtype']
            amount = float(r['amount'])
            entry = {
                "id": r['id'],
                "date": str(r['payment_date']),
                "amount": amount,
                "payment_mode": r['payment_mode'],
                "payment_type": r['payment_type'],
                "payment_subtype": subtype,
                "note": r['note'],
                "batch_id": r['batch_id'],
                "billing_month": r['billing_month'],
            }

            if subtype == 'DEPOSIT_RECEIVED':
                balance += amount
                totals["received"] += amount
                entry["balance_after"] = balance
            elif subtype == 'DEPOSIT_UTILIZED':
                balance -= amount
                totals["utilized"] += amount
                entry["balance_after"] = balance
            elif subtype == 'DEPOSIT_REFUNDED':
                balance -= amount
                totals["refunded"] += amount
                entry["balance_after"] = balance
            elif subtype == 'SETTLEMENT_EXTRA':
                # Cash from tenant at settlement - does NOT affect deposit balance
                totals["extra"] += amount
                entry["balance_after"] = balance
                entry["is_cash_in"] = True
            else:
                entry["balance_after"] = balance

            transactions.append(entry)

        # Ensure balance never negative (floor at 0 for display)
        display_balance = max(0.0, balance)

        return {
            "tenant_id": tenant_id,
            "tenant_name": tenant['tenant_name'],
            "required_deposit": float(tenant['security_deposit_applied'] or 0),
            "current_balance": display_balance,
            "raw_balance": balance,
            "totals": totals,
            "transactions": transactions,
        }
    except HTTPException:
        raise
    except Exception as e:
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
            SELECT tenant_id, billing_month, month_total, jama, jama_date, payment_status,
                   prev_baki, prev_jama
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
            # Shared carry-aware rule (mirrors _paid_status_case): Paid iff the
            # carry-inclusive outstanding is covered; a month fully covered by
            # carried-in credit alone (jama = 0) still reads Paid, while a
            # fully-empty month stays Unpaid.
            _prev_b = float(ledger['prev_baki'] or 0.00)
            _prev_j = float(ledger['prev_jama'] or 0.00)
            _net_left = _prev_b + month_total - _prev_j - new_jama
            _in_scope = (month_total > 0 or new_jama > 0 or _prev_b > 0 or _prev_j > 0)
            if _net_left <= 0 and _in_scope:
                new_status = "Paid"
            elif new_jama > 0:
                new_status = "Partial"
            else:
                new_status = "Unpaid"

        cur.execute("""
            UPDATE rental_core.monthly_ledgers
            SET jama = %s, jama_date = %s, payment_status = %s, settled_estimate = FALSE
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

        cur.execute(f"""
            UPDATE rental_core.monthly_ledgers
            SET manual_override = (
                    light_bill_manual IS NOT NULL OR gas_bill_manual IS NOT NULL
                    OR maintenance_manual IS NOT NULL OR common_misc_manual IS NOT NULL
                ),
                payment_status = CASE
                    {_paid_status_case()}
                    WHEN jama > 0 THEN 'Partial'
                    ELSE 'Unpaid'
                END,
                settled_estimate = FALSE
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
    country_code: Optional[str] = Field(default=None, pattern="^(91|977)$")

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
        for col in ["tenant_name", "room_id", "is_active",
                    "lease_start_date", "lease_end_date", "booking_amount_paid", "pending_deposit_due_date"]:
            val = getattr(data, col, None)
            if val is not None:
                updates[col] = val
        if data.phone_number is not None or data.country_code is not None:
            _cc = data.country_code or existing.get('country_code') or '91'
            _raw = data.phone_number if data.phone_number is not None else existing.get('phone_number')
            _cc, _digits = _split_phone(_cc, _raw)
            updates['country_code'] = _cc
            updates['phone_number'] = _digits
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
    silent: Optional[bool] = Field(default=False, description="Set the due date without queueing any WhatsApp message")


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
def list_reminders(month: str = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                   billing_month: str = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                   due_now: bool = Query(False)):
    """Reminder views (all include history; ACTIVE-first ordering):
    - billing_month=YYYY-MM: reminders booked against that billing cycle,
      whatever calendar month their due date falls in (Due Dates tab).
    - due_now=true: ACTIVE reminders due on/before end of current calendar
      month, any billing month (action list; anchored to today).
    - month=YYYY-MM (legacy): reminders whose DUE DATE falls in that month."""
    if billing_month:
        return _utc_tagged(rm.billing_month_reminders(billing_month))
    if due_now:
        return _utc_tagged(rm.due_now_reminders())
    if not month:
        raise HTTPException(status_code=422, detail="Provide month, billing_month, or due_now=true")
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
          AND ml.payment_status IS DISTINCT FROM 'Settled'
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
    from datetime import datetime as _dt
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

        # queue NOTIFY message (type-aware + payment ack) — skipped entirely
        # when silent (due date is still set, just no WhatsApp goes out).
        _notify_queued = False
        _msg, _link = None, None
        if not data.silent:
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
            _notify_queued = True
            _msg, _link = payload["notify"], payload["wa_link"]
        conn.commit()
        return {
            "status": "SUCCESS",
            "reminder": reminder,
            "message": _msg,
            "wa_link": _link,
            "notify_queued": _notify_queued,
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
    from datetime import datetime as _dt
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