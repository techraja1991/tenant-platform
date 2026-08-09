import os
import subprocess
import psycopg2
import psycopg2.extras
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
    default_base_rent: Optional[float] = 3300.00
    default_maintenance_fee: Optional[float] = 0.00
    default_security_deposit: Optional[float] = 5000.00
    default_common_utility: Optional[float] = 0.00

class RoomCreate(BaseModel):
    master_id: int
    flat_number: str
    display_address: str

class TenantCreate(BaseModel):
    room_id: int
    tenant_name: str
    phone_number: str
    base_rent_applied: float
    maintenance_applied: Optional[float] = 0.00
    security_deposit_applied: Optional[float] = 0.00

class MaintenanceExpenseCreate(BaseModel):
    master_id: int
    expense_name: str
    amount: float
    charge_type: Optional[str] = "SHARED"
    room_id: Optional[int] = None

class PaymentCreate(BaseModel):
    tenant_id: int
    billing_month: str
    payment_date: str
    amount: float
    payment_mode: Optional[str] = "CASH"
    payment_type: Optional[str] = "RENT"
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
def get_dashboard_stats():
    conn = get_db()
    cur = conn.cursor()
    execution_month = datetime.now().strftime('%Y-%m')

    cur.execute("SELECT COUNT(*) as count FROM rental_core.property_masters;")
    props = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.rooms;")
    rooms = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) as count FROM rental_core.tenants WHERE is_active = 'Y';")
    active_tenants = cur.fetchone()['count']

    cur.execute("""
        SELECT COALESCE(SUM(new_baki), 0.00) as total_outstanding 
        FROM rental_core.monthly_ledgers 
        WHERE billing_month = %s;
    """, (execution_month,))
    outstanding = cur.fetchone()['total_outstanding']

    cur.close()
    conn.close()
    return {
        "execution_month": execution_month,
        "properties": props,
        "rooms": rooms,
        "active_tenants": active_tenants,
        "total_outstanding_due": float(outstanding)
    }


# --- 2. PROPERTY MASTERS & ROOMS ---
@app.get("/api/properties")
def list_properties():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rental_core.property_masters ORDER BY id DESC;")
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

@app.get("/api/rooms")
def list_rooms():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.*, pm.apartment_name, pm.plot_number 
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


# --- 3. TENANT MANAGEMENT ---
@app.get("/api/tenants")
def list_tenants():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            t.id, 
            t.tenant_name, 
            t.phone_number, 
            t.is_active, 
            t.base_rent_applied, 
            t.maintenance_applied, 
            t.security_deposit_applied,
            t.lease_start_date,
            r.display_address 
        FROM rental_core.tenants t
        LEFT JOIN rental_core.rooms r ON t.room_id = r.id
        ORDER BY t.id DESC;
    """)
    tenants = cur.fetchall()
    cur.close()
    conn.close()
    return tenants

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
@app.get("/api/expenses")
def list_expenses():
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
@app.get("/api/ledgers/current")
def get_current_ledgers():
    execution_month = datetime.now().strftime('%Y-%m')
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
    """, (execution_month,))
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
    cur.execute("""
        UPDATE rental_core.monthly_ledgers
        SET jama = %s,
            jama_date = %s,
            payment_status = CASE
                WHEN month_total <= 0 THEN 'Unpaid'
                WHEN %s >= month_total THEN 'Paid'
                WHEN %s > 0 THEN 'Partial'
                ELSE 'Unpaid'
            END
        WHERE tenant_id = %s AND billing_month = %s
    """, (row['total_paid'], row['last_pay_date'], row['total_paid'], row['total_paid'],
          tenant_id, billing_month))


@app.get("/api/payments")
def list_payments(billing_month: str = None):
    if not billing_month:
        billing_month = datetime.now().strftime('%Y-%m')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.tenant_id, p.room_id, p.billing_month, p.payment_date, p.amount,
               p.payment_mode, p.payment_type, p.note, p.created_at,
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
        cur.execute("""
            INSERT INTO rental_core.tenant_payments
            (tenant_id, room_id, billing_month, payment_date, amount, payment_mode, payment_type, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
        """, (payment.tenant_id, room_id, payment.billing_month, payment.payment_date,
              payment.amount, payment.payment_mode, payment.payment_type, payment.note))
        new_id = cur.fetchone()['id']
        if payment.payment_type == "RENT":
            _sync_ledger_jama(cur, payment.tenant_id, payment.billing_month)
        conn.commit()
        return {"status": "SUCCESS", "payment_id": new_id}
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
        cur.execute("SELECT tenant_id, billing_month, payment_type FROM rental_core.tenant_payments WHERE id = %s",
                    (payment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Payment not found")
        cur.execute("DELETE FROM rental_core.tenant_payments WHERE id = %s", (payment_id,))
        if row['payment_type'] == 'RENT':
            _sync_ledger_jama(cur, row['tenant_id'], row['billing_month'])
        conn.commit()
        return {"status": "SUCCESS"}
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


# --- 7. ONE-CLICK OPERATIONAL ACTIONS ---
@app.post("/api/operations/run-torrent-scraping")
def trigger_torrent_scraping(background_tasks: BackgroundTasks):
    def run_scraper():
        subprocess.run(["python3", "torrent_automation_engine.py"], check=True)

    background_tasks.add_task(run_scraper)
    return {"status": "QUEUED", "message": "Torrent Power automation scraper triggered in background."}

@app.post("/api/operations/allocate-bills")
def trigger_bill_allocation():
    try:
        subprocess.run(["python3", "allocate_tenant_bills.py"], check=True)
        return {"status": "SUCCESS", "message": "Bills allocated and ledgers compiled successfully."}
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
    phone_number: str
    base_rent_applied: float
    maintenance_applied: Optional[float] = 0.00
    security_deposit_applied: Optional[float] = 0.00
    is_active: Optional[str] = 'Y'

@app.put("/api/tenants/{tenant_id}")
def update_tenant(tenant_id: int, data: TenantUpdate):
    """Updates active tenant details or rent terms."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE rental_core.tenants 
            SET phone_number = %s,
                base_rent_applied = %s,
                maintenance_applied = %s,
                security_deposit_applied = %s,
                is_active = %s
            WHERE id = %s;
        """, (data.phone_number, data.base_rent_applied, data.maintenance_applied, 
              data.security_deposit_applied, data.is_active, tenant_id))
        conn.commit()
        return {"status": "SUCCESS", "message": f"Tenant #{tenant_id} updated."}
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
        subprocess.run(["python3", "import_gas_metrics.py"], check=True)
        return {"status": "SUCCESS", "message": "Piped gas metrics imported from CSV successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))