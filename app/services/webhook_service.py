import hashlib
import hmac
import json
import logging
import random
import secrets
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from app.branding import APP_NAME

from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value
from app.security.safe_http import safe_httpx_post
from app.services import webhook_settings_service

logger = logging.getLogger(__name__)

WEBHOOK_EVENT_TYPES = (
    "activity_created",
    "activity_updated",
    "activity_heartbeat",
    "activity_cancelled",
    "activity_recovered",
    "connector_executed",
    "delegation_request_created",
    "delegation_request_approved",
    "delegation_request_denied",
)

DELIVERY_TIMEOUT_SECONDS = 5
LEASE_SECONDS = 30
POLL_SECONDS = 0.5
_worker_stop = threading.Event()
_worker: threading.Thread | None = None


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
            (
                webhook_id,
                name,
                url,
                secret_encrypted,
                json.dumps(valid_events),
                created_by,
                now,
                now,
            ),
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
        if enabled is False:
            conn.execute(
                "UPDATE webhook_delivery_log SET status = 'cancelled', error_message = 'Webhook disabled' "
                "WHERE webhook_id = ? AND status IN ('pending', 'retry_wait')",
                (webhook_id,),
            )
    return cursor.rowcount > 0


def delete_webhook(webhook_id: str) -> bool:
    with get_db() as conn:
        conn.execute(
            "UPDATE webhook_delivery_log SET status = 'cancelled', error_message = 'Webhook deleted' "
            "WHERE webhook_id = ? AND status IN ('pending', 'retry_wait')",
            (webhook_id,),
        )
        cursor = conn.execute(
            "DELETE FROM webhook_registrations WHERE id = ?", (webhook_id,)
        )
    return cursor.rowcount > 0


def list_deliveries(webhook_id: str, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, webhook_id, event_id, event_type, status, attempt_count, next_attempt_at,
                   last_attempt_at, http_status, error_message, created_at, delivered_at
            FROM webhook_delivery_log
            WHERE webhook_id = ?
            ORDER BY created_at DESC
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
# Delivery queue
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _enqueue_delivery(webhook_id: str, event_id: str, event_type: str, payload: dict) -> None:
    payload_json = json.dumps(payload, separators=(",", ":"))
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO webhook_delivery_log
                (webhook_id, event_id, event_type, payload_json, status, attempt_count, next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (webhook_id, event_id, event_type, payload_json, now, now),
            )
    except Exception:
        logger.exception("Failed to enqueue webhook delivery for %s", webhook_id)


def _claim_due_delivery() -> Optional[dict]:
    now = _iso()
    lease_expires = _iso(_now() + timedelta(seconds=LEASE_SECONDS))
    with get_db() as conn:
        conn.execute(
            "UPDATE webhook_delivery_log SET status = 'retry_wait', lease_expires_at = NULL "
            "WHERE status = 'delivering' AND lease_expires_at < ?",
            (now,),
        )
        row = conn.execute(
            "SELECT * FROM webhook_delivery_log WHERE status IN ('pending', 'retry_wait') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 1",
            (now,),
        ).fetchone()
        if not row:
            return None
        claimed = conn.execute(
            "UPDATE webhook_delivery_log SET status = 'delivering', attempt_count = attempt_count + 1, "
            "last_attempt_at = ?, lease_expires_at = ? WHERE id = ? AND status IN ('pending', 'retry_wait')",
            (now, lease_expires, row["id"]),
        )
        if not claimed.rowcount:
            return None
        return dict(conn.execute("SELECT * FROM webhook_delivery_log WHERE id = ?", (row["id"],)).fetchone())


def _retry_after_seconds(value: str | None, maximum: int) -> int | None:
    if not value:
        return None
    try:
        return min(max(int(value), 0), maximum)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(max(int((retry_at - _now()).total_seconds()), 0), maximum)


def _finish_delivery(delivery: dict) -> None:
    webhook_id = delivery["webhook_id"]
    payload_bytes = delivery["payload_json"].encode()
    event_type = delivery["event_type"]
    event_id = delivery["event_id"]
    with get_db() as conn:
        webhook = conn.execute(
            "SELECT url, secret_encrypted, enabled FROM webhook_registrations WHERE id = ?",
            (webhook_id,),
        ).fetchone()
    if not webhook or not webhook["enabled"]:
        _set_delivery(delivery["id"], "cancelled", error_message="Webhook disabled or deleted")
        return
    try:
        secret_plaintext = decrypt_value(webhook["secret_encrypted"])
    except Exception:
        _retry_or_dead(delivery, None, "Secret decryption failed")
        return

    signature = _sign_payload(secret_plaintext, payload_bytes)
    http_status = None
    error_message = None
    try:
        with httpx.Client(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            response = safe_httpx_post(
                client,
                webhook["url"],
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Core-Signature": signature,
                    "X-Agent-Core-Event": event_type,
                    "X-Agent-Core-Event-Id": event_id,
                },
            )
        http_status = response.status_code
        if 200 <= response.status_code < 300:
            _set_delivery(delivery["id"], "success", http_status=response.status_code, delivered_at=_iso())
            return
        error_message = f"HTTP {response.status_code}"
        _retry_or_dead(
            delivery,
            http_status,
            error_message,
            retry_after=response.headers.get("retry-after") if response.status_code == 429 else None,
        )
        return
    except httpx.TimeoutException:
        error_message = "Delivery timed out"
    except Exception as exc:
        error_message = str(exc)[:200]

    _retry_or_dead(delivery, http_status, error_message)


def _set_delivery(delivery_id: int, status: str, *, http_status: Optional[int] = None,
                  error_message: Optional[str] = None, next_attempt_at: Optional[str] = None,
                  delivered_at: Optional[str] = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE webhook_delivery_log SET status = ?, http_status = ?, error_message = ?, "
            "next_attempt_at = ?, lease_expires_at = NULL, delivered_at = ? WHERE id = ?",
            (status, http_status, error_message, next_attempt_at, delivered_at, delivery_id),
        )


def _retry_or_dead(
    delivery: dict,
    http_status: Optional[int],
    error_message: str,
    *,
    retry_after: str | None = None,
) -> None:
    retryable = http_status is None or http_status in {408, 429} or 500 <= http_status < 600
    policy = webhook_settings_service.retry_policy()
    if not retryable or delivery["attempt_count"] >= policy["webhook_retry_max_attempts"]:
        _set_delivery(delivery["id"], "dead", http_status=http_status, error_message=error_message, delivered_at=_iso())
        return
    delay = _retry_after_seconds(retry_after, policy["webhook_retry_max_seconds"])
    if delay is None:
        delay = min(
            policy["webhook_retry_max_seconds"],
            policy["webhook_retry_initial_seconds"] * 2 ** max(0, delivery["attempt_count"] - 1),
        )
    if policy["webhook_retry_jitter_seconds"]:
        delay = min(
            policy["webhook_retry_max_seconds"],
            delay + random.uniform(0, policy["webhook_retry_jitter_seconds"]),
        )
    _set_delivery(
        delivery["id"], "retry_wait", http_status=http_status, error_message=error_message,
        next_attempt_at=_iso(_now() + timedelta(seconds=delay)),
    )


def run_delivery_cycle() -> bool:
    delivery = _claim_due_delivery()
    if not delivery:
        return False
    _finish_delivery(delivery)
    return True


def _delivery_loop() -> None:
    while not _worker_stop.is_set():
        try:
            while run_delivery_cycle():
                pass
        except Exception:
            logger.exception("Webhook delivery worker failed")
        _worker_stop.wait(POLL_SECONDS)


def start_delivery_worker() -> threading.Thread:
    global _worker
    if _worker and _worker.is_alive():
        return _worker
    _worker_stop.clear()
    _worker = threading.Thread(target=_delivery_loop, name="webhook-delivery", daemon=True)
    _worker.start()
    return _worker


def stop_delivery_worker() -> None:
    _worker_stop.set()
    if _worker and _worker.is_alive():
        _worker.join(timeout=LEASE_SECONDS)


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

    timestamp = _iso()
    event_id = secrets.token_urlsafe(18)
    payload = {"event_id": event_id, "event_type": event_type, "timestamp": timestamp, "data": data}

    for row in rows:
        try:
            subscribed = json.loads(row["event_types_json"] or "[]")
        except Exception:
            continue
        if event_type not in subscribed:
            continue
        webhook_id = row["id"]
        _enqueue_delivery(webhook_id, event_id, event_type, payload)


def _sample_payload(event_type: str) -> dict:
    """Return a realistic sample data payload for a given event type."""
    now = datetime.now(timezone.utc).isoformat()
    started = "2026-01-15T10:00:00+00:00"
    if event_type in (
        "activity_created",
        "activity_updated",
        "activity_heartbeat",
        "activity_cancelled",
        "activity_recovered",
    ):
        status_map = {
            "activity_created": "active",
            "activity_updated": "active",
            "activity_heartbeat": "active",
            "activity_cancelled": "cancelled",
            "activity_recovered": "active",
        }
        data = {
            "activity_id": "sample-activity-id",
            "task_description": "Sample task: reviewing PR #42",
            "task_note": "Applied a sample progress update"
            if event_type == "activity_updated"
            else None,
            "task_result": None,
            "agent_id": "my-agent",
            "assigned_agent_id": "my-agent",
            "user_id": "admin",
            "memory_scope": "workspace:my-project",
            "status": status_map[event_type],
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
    elif event_type in (
        "delegation_request_created",
        "delegation_request_approved",
        "delegation_request_denied",
    ):
        status_map = {
            "delegation_request_created": "pending",
            "delegation_request_approved": "approved",
            "delegation_request_denied": "denied",
        }
        decided = event_type != "delegation_request_created"
        data = {
            "request_id": "sample-request-id",
            "status": status_map[event_type],
            "requester_actor_type": "agent",
            "requester_actor_id": "coordinator-agent",
            "recipient_agent_id": "worker-agent",
            "target_user_id": "admin",
            "purpose": "Summarize this week's workspace decisions",
            "ttl_seconds": 900,
            "scope_permission_count": 1,
            "resource_permission_count": 0,
            "binding_action_count": 0,
            "decided_by_actor_id": "admin" if decided else None,
            "decision_reason": "not needed"
            if event_type == "delegation_request_denied"
            else None,
            "grant_id": "sample-grant-id"
            if event_type == "delegation_request_approved"
            else None,
            "created_at": started,
            "decided_at": now if decided else None,
        }
    else:
        data = {
            "message": f"{APP_NAME} webhook test delivery",
            "event_type": event_type,
        }
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
    event_id = secrets.token_urlsafe(18)
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "data": _sample_payload(event_type),
    }
    _enqueue_delivery(webhook_id, event_id, event_type, payload)
    with get_db() as conn:
        delivery = conn.execute(
            "SELECT * FROM webhook_delivery_log WHERE webhook_id = ? AND event_id = ?",
            (webhook_id, event_id),
        ).fetchone()
    if not delivery:
        return {"ok": False, "error": "Could not create delivery"}
    _finish_delivery(dict(delivery))
    with get_db() as conn:
        finished = conn.execute(
            "SELECT status, http_status, error_message FROM webhook_delivery_log WHERE id = ?",
            (delivery["id"],),
        ).fetchone()
    if finished and finished["status"] == "success":
        return {"ok": True, "http_status": finished["http_status"], "event_type": event_type}
    return {
        "ok": False,
        "http_status": finished["http_status"] if finished else None,
        "error": finished["error_message"] if finished else "Delivery failed",
    }


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
