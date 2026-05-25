import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from app.branding import ENV_PREFIX
from app.config import settings

DB_PATH_OVERRIDE: str | None = os.environ.get(f"{ENV_PREFIX}TEST_DB")


def get_db_path() -> Path:
    if DB_PATH_OVERRIDE:
        return Path(DB_PATH_OVERRIDE)
    return settings.db_path


def reset_test_db(path: str) -> None:
    global DB_PATH_OVERRIDE
    DB_PATH_OVERRIDE = path


def init_test_db() -> None:
    init_db()


_sqlite_vec_available: bool | None = None


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    global _sqlite_vec_available
    if _sqlite_vec_available is False:
        return
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _sqlite_vec_available = True
    except Exception:
        _sqlite_vec_available = False


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_sqlite_vec(conn)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    from app.schema import create_schema

    with get_db() as conn:
        create_schema(conn)
