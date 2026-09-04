"""Typed reads and atomic writes for the system_settings key/value store."""

import sqlite3
from collections.abc import Mapping, Sequence

from app.database import get_db


def read_raw(defaults: Mapping[str, str]) -> dict[str, str]:
    result = dict(defaults)
    keys = tuple(defaults)
    if not keys:
        return result
    placeholders = ",".join("?" for _ in keys)
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM system_settings WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        result[row["key"]] = row["value"]
    return result


def read_string(key: str, default: str = "") -> str:
    return read_raw({key: default})[key] or default


def read_int(key: str, default: int) -> int:
    try:
        return int(read_string(key, str(default)))
    except (TypeError, ValueError):
        return default


def read_float(key: str, default: float) -> float:
    try:
        return float(read_string(key, str(default)))
    except (TypeError, ValueError):
        return default


def read_bool(key: str, default: bool = False) -> bool:
    value = read_string(key, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def write_raw(values: Mapping[str, str]) -> None:
    if not values:
        return
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            [(key, value) for key, value in values.items()],
        )
        conn.commit()


def delete(keys: Sequence[str]) -> None:
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    with get_db() as conn:
        conn.execute(f"DELETE FROM system_settings WHERE key IN ({placeholders})", tuple(keys))
        conn.commit()
