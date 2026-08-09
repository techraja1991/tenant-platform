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

def get_db_connection(as_dict: bool = False):
    """
    Establishes and returns a active PostgreSQL database connection.
    
    :param as_dict: If True, returns cursor results as RealDictCursor (key-value dictionaries).
    """
    params = get_db_params()
    if as_dict:
        return psycopg2.connect(**params, cursor_factory=psycopg2.extras.RealDictCursor)
    return psycopg2.connect(**params)