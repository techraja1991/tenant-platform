import os
import sys
import subprocess
import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from db_config import get_db_connection

app = FastAPI(title="Tenant Management Admin Core API", version="2.0.0")
import base64
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

AUTH_USER = os.getenv("AUTH_USER", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")

class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                user, _, pw = decoded.partition(":")
                if user == AUTH_USER and pw == AUTH_PASSWORD:
                    return await call_next(request)
            except Exception:
                pass
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
    execution_month = datetime.now().strftime('%Y-%m')

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
        "carried_forward": float(carried_forward),
        "older_outstanding": float(older_outstanding),
        "outstanding_by_property": [
            {"apartment_name": p['apartment_name'], "outstanding": float(p['outstanding']), "units": p['units']}
            for p in outstanding_by_property
        ]
    }


# --- 2. PROPERTY MASTERS & ROOMS ---
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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


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

@app.post("/api/tenants")
def create_tenant(tenant: TenantCreate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO rental_core.tenants 
            (room_id, tenant_name, phone_number, base_rent_applied, maintenance_applied, security_deposit_applied, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, 'Y') RETURNING id;
        """, (
            tenant.room_id, tenant.tenant_name, tenant.phone_number, 
            tenant.base_rent_applied, tenant.maintenance_applied, tenant.security_deposit_applied
        ))
        new_id = cur.fetchone()['id']
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
        execution_month = datetime.now().strftime('%Y-%m')
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
    execution_month = datetime.now().strftime('%Y-%m')
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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


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
    """Distinct billing months available in the ledgers, newest first."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT billing_month
        FROM rental_core.monthly_ledgers
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
        billing_month = datetime.now().strftime('%Y-%m')
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
            t.tenant_name,
            t.phone_number
        FROM rental_core.monthly_ledgers ml
        LEFT JOIN rental_core.rooms r ON ml.room_id = r.id
        LEFT JOIN rental_core.tenants t ON ml.tenant_id = t.id
        WHERE ml.billing_month = %s
        ORDER BY r.display_address ASC;
    """, (billing_month,))
    ledgers = cur.fetchall()
    cur.close()
    conn.close()
    return ledgers


# --- 6b. TENANT PAYMENTS (rent + deposit) ---

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
        # month is compiled: carry forward prior month balance as prev_baki.
        cur.execute("""
            INSERT INTO rental_core.monthly_ledgers
                (room_id, tenant_id, billing_month, prev_baki, prev_jama, bhadu_applied,
                 light_bill, gas_bill, maintenance_applied, common_misc_expenses)
            SELECT t.room_id, t.id, %s,
                   COALESCE((SELECT new_baki FROM rental_core.monthly_ledgers
                             WHERE tenant_id = t.id AND billing_month < %s
                             ORDER BY billing_month DESC LIMIT 1), 0.00),
                   0.00, 0.00, 0.00, 0.00, 0.00, 0.00
            FROM rental_core.tenants t WHERE t.id = %s
            ON CONFLICT (tenant_id, billing_month) DO NOTHING
        """, (billing_month, billing_month, tenant_id))

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
    next_prev_baki = float(base['new_baki'])
    next_prev_jama = float(base['prev_jama'])

    for bm in months:
        cur.execute("""
            UPDATE rental_core.monthly_ledgers
            SET prev_baki = %s, prev_jama = %s
            WHERE tenant_id = %s AND billing_month = %s
            RETURNING COALESCE(new_baki, 0.00) AS new_baki,
                      COALESCE(prev_jama, 0.00) AS prev_jama
        """, (next_prev_baki, next_prev_jama, tenant_id, bm))
        row = cur.fetchone()
        if row:
            next_prev_baki = float(row['new_baki'])
            next_prev_jama = float(row['prev_jama'])


@app.get("/api/payments")
def list_payments(billing_month: str = None):
    if not billing_month:
        billing_month = datetime.now().strftime('%Y-%m')
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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


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

        # Deleting any row of a split payment deletes the whole batch
        cur.execute("SELECT tenant_id, billing_month FROM rental_core.tenant_payments WHERE batch_id = %s",
                    (row['batch_id'],))
        batch_rows = cur.fetchall()
        cur.execute("DELETE FROM rental_core.tenant_payments WHERE batch_id = %s", (row['batch_id'],))
        for b in batch_rows:
            _sync_ledger_jama(cur, b['tenant_id'], b['billing_month'])
        conn.commit()
        return {"status": "SUCCESS", "deleted_payments": len(batch_rows)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


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
        return FileResponse("static/index.html")
    raise HTTPException(status_code=404, detail="Frontend static/index.html not found.")



class TenantUpdate(BaseModel):
    tenant_name: Optional[str] = None
    room_id: Optional[int] = None
    phone_number: Optional[str] = None
    base_rent_applied: Optional[float] = Field(default=None, ge=0)
    maintenance_applied: Optional[float] = Field(default=None, ge=0)
    security_deposit_applied: Optional[float] = Field(default=None, ge=0)
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
                    "lease_start_date", "lease_end_date"]:
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
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.post("/api/operations/import-gas-metrics")
def trigger_gas_import():
    """Triggers the Gujarat Gas CSV parser script."""
    try:
        subprocess.run([sys.executable, "import_gas_metrics.py"], check=True)
        return {"status": "SUCCESS", "message": "Piped gas metrics imported from CSV successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))