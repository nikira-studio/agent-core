import secrets
from typing import Optional
from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value
from app.time_utils import utc_now_iso


VECTOR_KEYS = (
    "vector_search_enabled",
    "vector_provider",
    "vector_url",
    "vector_model",
    "vector_dimension",
    "vector_auth_type",
)

VECTOR_DEFAULTS = {
    "vector_search_enabled": "false",
    "vector_provider": "ollama",
    "vector_url": "http://localhost:11434",
    "vector_model": "nomic-embed-text",
    "vector_dimension": "768",
    "vector_auth_type": "none",
}


def get_vector_settings() -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM system_settings WHERE key IN (?, ?, ?, ?, ?, ?)",
            list(VECTOR_KEYS),
        ).fetchall()
        result = dict(VECTOR_DEFAULTS)
        for row in rows:
            result[row["key"]] = row["value"]
        api_key_plaintext = _get_stored_api_key_plaintext()
        result["vector_api_key"] = api_key_plaintext if api_key_plaintext else ""
        result["vector_has_api_key"] = bool(api_key_plaintext)
        return result


def get_vector_setting(key: str) -> str:
    if key not in VECTOR_KEYS and key != "vector_api_key":
        raise ValueError(f"Unknown vector setting: {key}")
    if key == "vector_api_key":
        return _get_stored_api_key_plaintext() or ""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else VECTOR_DEFAULTS.get(key, "")


def is_vector_search_enabled() -> bool:
    return get_vector_setting("vector_search_enabled").lower() == "true"


def get_vector_url() -> str:
    return get_vector_setting("vector_url")


def get_vector_model() -> str:
    return get_vector_setting("vector_model")


def get_vector_dimension() -> int:
    try:
        return int(get_vector_setting("vector_dimension"))
    except ValueError:
        return 768


def get_vector_auth_type() -> str:
    return get_vector_setting("vector_auth_type")


def get_vector_provider() -> str:
    return get_vector_setting("vector_provider")


def has_vector_api_key() -> bool:
    return bool(_get_stored_api_key_plaintext())


def _system_settings_columns() -> set[str]:
    with get_db() as conn:
        return {
            row["name"]
            for row in conn.execute("PRAGMA table_info(system_settings)").fetchall()
        }


def _get_vector_key_record_id() -> Optional[str]:
    columns = _system_settings_columns()
    with get_db() as conn:
        if "id" in columns:
            row = conn.execute(
                "SELECT id FROM system_settings WHERE key = ?", ("vector_api_key",)
            ).fetchone()
            return row["id"] if row else None
        row = conn.execute(
            "SELECT key FROM system_settings WHERE key = ?", ("vector_api_key",)
        ).fetchone()
        return row["key"] if row else None


def _get_stored_api_key_plaintext() -> Optional[str]:
    record_id = _get_vector_key_record_id()
    if not record_id:
        return None
    columns = _system_settings_columns()
    with get_db() as conn:
        if "value_encrypted" in columns:
            key_column = "id" if "id" in columns else "key"
            row = conn.execute(
                f"SELECT value_encrypted FROM system_settings WHERE {key_column} = ?",
                (record_id,),
            ).fetchone()
            if not row or not row["value_encrypted"]:
                return None
            return decrypt_value(row["value_encrypted"])
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", ("vector_api_key",)
        ).fetchone()
        if not row or not row["value"]:
            return None
        try:
            return decrypt_value(row["value"])
        except Exception:
            return row["value"]


def save_vector_setting(key: str, value: str) -> bool:
    if key not in VECTOR_KEYS:
        raise ValueError(f"Unknown vector setting: {key}")
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        conn.commit()
        return cursor.rowcount > 0


def save_vector_api_key(api_key: str) -> bool:
    if not api_key.strip():
        return clear_vector_api_key()
    encrypted = encrypt_value(api_key.strip())
    record_id = _get_vector_key_record_id()
    now = utc_now_iso()
    columns = _system_settings_columns()
    with get_db() as conn:
        if "value_encrypted" in columns:
            if record_id:
                key_column = "id" if "id" in columns else "key"
                conn.execute(
                    f"UPDATE system_settings SET value_encrypted = ?, updated_at = ? WHERE {key_column} = ?",
                    (encrypted, now, record_id),
                )
            elif "id" in columns:
                entry_id = secrets.token_urlsafe(16)
                conn.execute(
                    "INSERT INTO system_settings (id, key, value, value_encrypted, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (entry_id, "vector_api_key", "", encrypted, now),
                )
            else:
                conn.execute(
                    "INSERT INTO system_settings (key, value, value_encrypted, updated_at) VALUES (?, ?, ?, ?)",
                    ("vector_api_key", "", encrypted, now),
                )
        else:
            conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                ("vector_api_key", encrypted, now),
            )
        conn.commit()
    return True


def clear_vector_api_key() -> bool:
    record_id = _get_vector_key_record_id()
    if not record_id:
        return True
    columns = _system_settings_columns()
    key_column = "id" if "id" in columns else "key"
    with get_db() as conn:
        conn.execute(
            f"DELETE FROM system_settings WHERE {key_column} = ?", (record_id,)
        )
        conn.commit()
    return True


def save_vector_settings(
    enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    url: Optional[str] = None,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
    auth_type: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    if enabled is not None:
        save_vector_setting("vector_search_enabled", "true" if enabled else "false")
    if provider is not None:
        if provider not in ("ollama", "generic"):
            raise ValueError("provider must be ollama or generic")
        save_vector_setting("vector_provider", provider)
    if url is not None:
        save_vector_setting("vector_url", url.strip().rstrip("/"))
    if model is not None:
        save_vector_setting("vector_model", model.strip())
    if dimension is not None:
        save_vector_setting("vector_dimension", str(int(dimension)))
    if auth_type is not None:
        if auth_type not in ("none", "bearer", "api_key"):
            raise ValueError("auth_type must be none, bearer, or api_key")
        save_vector_setting("vector_auth_type", auth_type)
    if api_key is not None:
        save_vector_api_key(api_key)
    try:
        from app.services.embedding_service import _reset_cache

        _reset_cache()
    except Exception:
        pass
    return get_vector_settings()


def test_vector_connection() -> dict:
    from app.services import embedding_service

    status = embedding_service.get_embedding_backend_status()
    if status.get("backend") == "disabled":
        return {
            "success": False,
            "error": "Vector search is disabled. Enable it first.",
        }
    if status.get("backend") == "unavailable":
        return {
            "success": False,
            "error": f"Cannot reach embedding backend at {status.get('vector_url', 'unknown')}",
        }
    if not status.get("model_configured"):
        return {
            "success": False,
            "error": f"Model '{status.get('model', 'unknown')}' not found on backend",
        }
    return {
        "success": True,
        "backend": status.get("backend"),
        "model": status.get("model"),
    }
