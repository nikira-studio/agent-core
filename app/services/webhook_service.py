import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)

WEBHOOK_EVENT_TYPES = (
    "activity_created",
    "activity_updated",
    "activity_heartbeat",
    "activity_cancelled",
    "activity_recovered",
    "connector_executed",
)

DELIVERY_TIMEOUT_SECONDS = 5


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def create_webhook(
    name: str,
    url: str,
    secret_plaintext: str,
    event_types: list[str],
    created_by: str,
) -> dict:
    webhook_id = secrets.token_urlsafe(16)
    secret_encrypted = encrypt_value(secret_plaintext)
    now = datetime.now(timezone.utc).isoformat()
    valid_events = [e for e in event_types if e in WEBHOOK_EVENT_TYPES]
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO webhook_registrations
            (id, name, url, secret_encrypted, event_types_json, enabled, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (webhook_id, name, url, secret_encrypted, json.dumps(valid_events), created_by, now, now),
        )
    return get_webhook(webhook_id)


def list_webhooks() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, url, event_types_json, enabled, created_by, created_at, updated_at
            FROM webhook_registrations
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_row_to_webhook(dict(r)) for r in rows]


def get_webhook(webhook_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, name, url, event_types_json, enabled, created_by, created_at, updated_at
            FROM webhook_registrations WHERE id = ?
            """,
            (webhook_id,),
        ).fetchone()
    return _row_to_webhook(dict(row)) if row else None


def update_webhook(
    webhook_id: str,
    name: Optional[str] = None,
    url: Optional[str] = None,
    secret_plaintext: Optional[str] = None,
    event_types: Optional[list[str]] = None,
    enabled: Optional[bool] = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    updates = ["updated_at = ?"]
    params: list = [now]

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if url is not None:
        updates.append("url = ?")
        params.append(url)
    if secret_plaintext is not None:
        updates.append("secret_encrypted = ?")
        params.append(encrypt_value(secret_plaintext))
    if event_types is not None:
        valid_events = [e for e in event_types if e in WEBHOOK_EVENT_TYPES]
        updates.append("event_types_json = ?")
        params.append(json.dumps(valid_events))
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)

    params.append(webhook_id)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE webhook_registrations SET {', '.join(updates)} WHERE id = ?",
            params,
        )
    return cursor.rowcount > 0


def delete_webhook(webhook_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM webhook_registrations WHERE id = ?", (webhook_id,)
        )
    return cursor.rowcount > 0


def list_deliveries(webhook_id: str, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, webhook_id, event_type, status, http_status, error_message, delivered_at
            FROM webhook_delivery_log
            WHERE webhook_id = ?
            ORDER BY delivered_at DESC
            LIMIT ?
            """,
            (webhook_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _sign_payload(secret_plaintext: str, body: bytes) -> str:
    mac = hmac.new(secret_plaintext.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _record_delivery(
    webhook_id: str,
    event_type: str,
    payload_json: str,
    status: str,
    http_status: Optional[int],
    error_message: Optional[str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO webhook_delivery_log
                (webhook_id, event_type, payload_json, status, http_status, error_message, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (webhook_id, event_type, payload_json, status, http_status, error_message, now),
            )
    except Exception:
        logger.exception("Failed to record webhook delivery log for %s", webhook_id)


def _deliver_one(webhook_id: str, url: str, secret_encrypted: str, event_type: str, payload: dict) -> None:
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_bytes = payload_json.encode()
    try:
        secret_plaintext = decrypt_value(secret_encrypted)
    except Exception:
        logger.error("Failed to decrypt webhook secret for %s", webhook_id)
        _record_delivery(webhook_id, event_type, payload_json, "failure", None, "Secret decryption failed")
        return

    signature = _sign_payload(secret_plaintext, payload_bytes)
    http_status = None
    error_message = None
    status = "failure"
    try:
        response = httpx.post(
            url,
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Core-Signature": signature,
                "X-Agent-Core-Event": event_type,
            },
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
        http_status = response.status_code
        if 200 <= response.status_code < 300:
            status = "success"
        else:
            error_message = f"HTTP {response.status_code}"
    except httpx.TimeoutException:
        error_message = "Delivery timed out"
    except Exception as exc:
        error_message = str(exc)[:200]

    _record_delivery(webhook_id, event_type, payload_json, status, http_status, error_message)
    if status == "failure":
        logger.warning("Webhook delivery failed for %s event=%s: %s", webhook_id, event_type, error_message)


def dispatch_event(event_type: str, data: dict) -> None:
    """Dispatch a domain event to all enabled, subscribed webhooks. Non-blocking."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, url, secret_encrypted, event_types_json
                FROM webhook_registrations
                WHERE enabled = 1
                """
            ).fetchall()
    except Exception:
        logger.exception("Failed to query webhooks for dispatch")
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {"event_type": event_type, "timestamp": timestamp, "data": data}

    for row in rows:
        try:
            subscribed = json.loads(row["event_types_json"] or "[]")
        except Exception:
            continue
        if event_type not in subscribed:
            continue
        webhook_id = row["id"]
        url = row["url"]
        secret_encrypted = row["secret_encrypted"]
        t = threading.Thread(
            target=_deliver_one,
            args=(webhook_id, url, secret_encrypted, event_type, payload),
            daemon=True,
        )
        t.start()


def _sample_payload(event_type: str) -> dict:
    """Return a realistic sample data payload for a given event type."""
    now = datetime.now(timezone.utc).isoformat()
    started = "2026-01-15T10:00:00+00:00"
    if event_type in ("activity_created", "activity_updated", "activity_heartbeat",
                       "activity_cancelled", "activity_recovered"):
        data = {
            "activity_id": "sample-activity-id",
            "task_description": "Sample task: reviewing PR #42",
            "agent_id": "my-agent",
            "assigned_agent_id": "my-agent",
            "user_id": "admin",
            "memory_scope": "workspace:my-project",
            "status": {
                "activity_created": "active",
                "activity_updated": "active",
                "activity_heartbeat": "active",
                "activity_cancelled": "cancelled",
                "activity_recovered": "active",
            }[event_type],
            "started_at": started,
            "updated_at": now,
            "heartbeat_at": now,
            "ended_at": now if event_type == "activity_cancelled" else None,
            "previous_status": "active" if event_type != "activity_created" else None,
        }
    elif event_type == "connector_executed":
        data = {
            "binding_id": "sample-binding-id",
            "binding_name": "My API Binding",
            "scope": "workspace:my-project",
            "connector_type_id": "my-api",
            "connector_type_name": "My API",
            "action": "GET /status",
            "success": True,
            "duration_ms": 142,
            "status": "success",
            "error_message": None,
        }
    else:
        data = {"message": "Agent Core webhook test delivery", "event_type": event_type}
    return data


def test_delivery(webhook_id: str, event_type: Optional[str] = None) -> dict:
    """Send a synthetic test payload to the webhook. Returns delivery result."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, url, secret_encrypted, event_types_json FROM webhook_registrations WHERE id = ?",
            (webhook_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "Webhook not found"}

    # A test delivery always sends a synthetic "test" event, never a replay of a
    # real/subscribed event, so operators can verify wiring without emitting a
    # payload that a receiver might treat as a genuine event.
    if event_type is None:
        event_type = "test"
    elif event_type not in WEBHOOK_EVENT_TYPES:
        return {"ok": False, "error": f"Unknown event type: {event_type}"}

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_type": event_type,
        "timestamp": timestamp,
        "data": _sample_payload(event_type),
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_bytes = payload_json.encode()

    try:
        secret_plaintext = decrypt_value(row["secret_encrypted"])
    except Exception:
        return {"ok": False, "error": "Secret decryption failed"}

    signature = _sign_payload(secret_plaintext, payload_bytes)
    try:
        response = httpx.post(
            row["url"],
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Core-Signature": signature,
                "X-Agent-Core-Event": event_type,
            },
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
        status = "success" if 200 <= response.status_code < 300 else "failure"
        error = None if status == "success" else f"HTTP {response.status_code}"
        _record_delivery(webhook_id, event_type, payload_json, status, response.status_code, error)
        return {"ok": True, "http_status": response.status_code, "event_type": event_type}
    except httpx.TimeoutException:
        _record_delivery(webhook_id, event_type, payload_json, "failure", None, "Delivery timed out")
        return {"ok": False, "error": "Delivery timed out"}
    except Exception as exc:
        msg = str(exc)[:200]
        _record_delivery(webhook_id, event_type, payload_json, "failure", None, msg)
        return {"ok": False, "error": msg}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_webhook(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "event_types": json.loads(row.get("event_types_json") or "[]"),
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
