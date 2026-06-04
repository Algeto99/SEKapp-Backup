import logging
import os

import psycopg2

logger = logging.getLogger(__name__)


def get_db_connection():
    """Return a new psycopg2 connection using DATABASE_URL.

    psycopg2 accepts the full postgres:// URL directly, including Cloud SQL
    Unix-socket paths embedded in the URL query string, so no manual URL
    parsing is needed.

    Raises RuntimeError if DATABASE_URL is unset.
    Returns None if the connection attempt fails (so callers can do
    ``if not conn:`` checks without try/except at every call site).
    """
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable not set")
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        return None
