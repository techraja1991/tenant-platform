import json
import os
from pathlib import Path
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# Ensure .env is loaded using an absolute path relative to this file
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

def get_db_params() -> dict:
    """Returns database configuration dictionary, read exclusively from .env."""
    required = ["DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Missing required database settings in .env: {', '.join(missing)}"
        )
    return {
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT")
    }

def _cell(row, key):
    """Read one column from either a tuple cursor row or a RealDictRow."""
    if row is None:
        return None
    if isinstance(row, (list, tuple)):
        return row[0]
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def promote_settled_estimate(cursor, tenant_pk, billing_month, actual_light, actual_gas):
    """Estimate true-up after actual utility bills arrive for a settled month.

    If the month carries settled_estimate=TRUE, compare the compiled actuals
    against the estimate snapshot in the tenant's settlement history. On match
    (both utilities within ₹0.01) promote to plain Settled (flag FALSE);
    otherwise keep the flag so the UI shows 'Settled (Est.)' + variance.
    Returns True when promoted, False otherwise. Works with tuple or dict
    cursors. Raises nothing — callers run it inside their own transaction.
    """
    cursor.execute(
        """
        SELECT settled_estimate FROM rental_core.monthly_ledgers
        WHERE tenant_id = %s AND billing_month = %s
        """,
        (tenant_pk, billing_month)
    )
    if not _cell(cursor.fetchone(), 'settled_estimate'):
        return False
    cursor.execute(
        "SELECT settlement_history FROM rental_core.tenants WHERE id = %s",
        (tenant_pk,)
    )
    raw = _cell(cursor.fetchone(), 'settlement_history')
    try:
        hist = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        return False
    snap = next((h for h in hist
                 if isinstance(h, dict) and h.get('billing_month') == billing_month
                 and h.get('used_estimates')), None)
    if snap is None:
        return False
    if snap.get('est_light') is not None and abs(float(actual_light) - float(snap['est_light'])) > 0.01:
        return False
    if snap.get('est_gas') is not None and abs(float(actual_gas) - float(snap['est_gas'])) > 0.01:
        return False
    cursor.execute(
        """
        UPDATE rental_core.monthly_ledgers SET settled_estimate = FALSE
        WHERE tenant_id = %s AND billing_month = %s
        """,
        (tenant_pk, billing_month)
    )
    return True


def get_db_connection(as_dict: bool = False):
    """
    Establishes and returns a active PostgreSQL database connection.
    
    :param as_dict: If True, returns cursor results as RealDictCursor (key-value dictionaries).
    """
    params = get_db_params()
    if as_dict:
        return psycopg2.connect(**params, cursor_factory=psycopg2.extras.RealDictCursor)
    return psycopg2.connect(**params)