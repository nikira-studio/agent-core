import json
import threading
from typing import Optional

from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def binding_lock(binding_id: str) -> threading.Lock:
    """Per-binding refresh mutex. In-process by design (single-core; see plan.md §1/§8.3).
    Swap for a DB `locked_until` compare-and-set only if Agent Core ever goes multi-process."""
    with _locks_guard:
        lk = _locks.get(binding_id)
        if lk is None:
            lk = threading.Lock()
            _locks[binding_id] = lk
        return lk


def load_session(binding_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT session_data_encrypted FROM connector_session_cache WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
    if not row or not row["session_data_encrypted"]:
        return None
    try:
        return json.loads(decrypt_value(row["session_data_encrypted"]))
    except Exception:
        return None


def save_session(
    binding_id: str, session: Optional[dict], expires_at: Optional[str] = None
) -> None:
    if session is None:
        clear_session(binding_id)
        return
    blob = encrypt_value(json.dumps(session))
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_session_cache (binding_id, session_data_encrypted, expires_at, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(binding_id) DO UPDATE SET
                session_data_encrypted = excluded.session_data_encrypted,
                expires_at = excluded.expires_at, updated_at = CURRENT_TIMESTAMP
            """,
            (binding_id, blob, expires_at),
        )
        conn.commit()


def clear_session(binding_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM connector_session_cache WHERE binding_id = ?", (binding_id,)
        )
        conn.commit()
