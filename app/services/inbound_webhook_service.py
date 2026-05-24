"""Inbound webhook service — installation-wide key, 5 command event types."""
import hashlib
import json
import secrets
from typing import Any, Optional

from app.database import get_db
from app.security.scope_utils import normalize_scope_string
from app.services import activity_service, audit_service

INBOUND_KEY_PREFIX = "ac_inbound_"

INBOUND_EVENT_TYPES = frozenset(
    {
        "activity.create",
        "activity.assign",
        "activity.update",
        "activity.cancel",
        "activity.note",
    }
)

_ACTIVITY_MODIFY_EVENTS = frozenset(
    {"activity.assign", "activity.update", "activity.cancel", "activity.note"}
)


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def get_active_key_row() -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, key_hash, created_at, rotated_at FROM inbound_webhook_keys WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def has_active_key() -> bool:
    return get_active_key_row() is not None


def generate_key() -> str:
    """Generate the first inbound key. Raises if one already exists."""
    if has_active_key():
        raise ValueError("An active inbound key already exists; use rotate_key() instead")

    plaintext = f"{INBOUND_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(plaintext)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO inbound_webhook_keys (id, key_hash, is_active) VALUES (?, ?, 1)",
            (secrets.token_urlsafe(16), key_hash),
        )
        conn.commit()

    return plaintext


def rotate_key() -> str:
    """Deactivate the current key and issue a new one. Raises if no key exists yet."""
    existing = get_active_key_row()
    if not existing:
        raise ValueError("No active key to rotate; use generate_key() first")

    plaintext = f"{INBOUND_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(plaintext)

    with get_db() as conn:
        conn.execute(
            "UPDATE inbound_webhook_keys SET is_active = 0, rotated_at = CURRENT_TIMESTAMP WHERE is_active = 1"
        )
        conn.execute(
            "INSERT INTO inbound_webhook_keys (id, key_hash, is_active) VALUES (?, ?, 1)",
            (secrets.token_urlsafe(16), key_hash),
        )
        conn.commit()

    return plaintext


def verify_key(plaintext: str) -> bool:
    row = get_active_key_row()
    if not row:
        return False
    return secrets.compare_digest(_hash_key(plaintext), row["key_hash"])


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def _assert_workspace_match(activity_id: str, payload: dict) -> None:
    """Defense-in-depth scope check for modify events.

    The v1 inbound key is installation-wide, so it can address any activity. When a
    caller *does* assert a `workspace`, refuse to mutate an activity that belongs to a
    different workspace — this stops an integration scoped to one workspace from
    altering another's activities by id. Activities with no scope binding are left
    addressable, matching v1 behavior.
    """
    requested = payload.get("workspace")
    if not requested:
        return
    activity = activity_service.get_activity(activity_id)
    if not activity:
        raise ValueError(f"Activity not found: {activity_id}")
    target = activity.get("memory_scope")
    if not target and activity.get("metadata_json"):
        try:
            target = json.loads(activity["metadata_json"]).get("workspace")
        except (ValueError, TypeError):
            target = None
    if target and normalize_scope_string(requested) != normalize_scope_string(target):
        raise PermissionError(
            f"workspace {requested} does not match the target activity's scope"
        )


def handle_inbound(
    event_type: str,
    payload: dict[str, Any],
    ip_address: Optional[str] = None,
) -> dict[str, Any]:
    """
    Validate and execute one inbound command. Returns a result dict.
    Raises ValueError for unknown event types or missing required fields.
    Raises PermissionError when an asserted workspace does not match the target.
    """
    if event_type not in INBOUND_EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")

    if event_type in _ACTIVITY_MODIFY_EVENTS and not payload.get("activity_id"):
        raise ValueError(f"{event_type} requires activity_id")

    if event_type in _ACTIVITY_MODIFY_EVENTS:
        _assert_workspace_match(payload["activity_id"], payload)

    if event_type == "activity.create":
        return _handle_create(payload, ip_address)
    if event_type == "activity.assign":
        return _handle_assign(payload, ip_address)
    if event_type == "activity.update":
        return _handle_update(payload, ip_address)
    if event_type == "activity.cancel":
        return _handle_cancel(payload, ip_address)
    if event_type == "activity.note":
        return _handle_note(payload, ip_address)

    raise ValueError(f"Unhandled event type: {event_type}")  # unreachable


def _handle_create(payload: dict, ip_address: Optional[str]) -> dict:
    agent_id = payload.get("assigned_agent_id")
    if not agent_id:
        raise ValueError("activity.create requires assigned_agent_id")

    task_description = payload.get("task_description", "")
    memory_scope = payload.get("memory_scope")
    workspace = payload.get("workspace")
    metadata: dict = {}
    if workspace:
        metadata["workspace"] = workspace

    activity = activity_service.create_activity(
        agent_id=agent_id,
        user_id="inbound-webhook",
        task_description=task_description,
        memory_scope=memory_scope,
        metadata_json=json.dumps(metadata) if metadata else None,
    )

    audit_service.write_event(
        actor_type="system",
        actor_id="inbound-webhook",
        action="inbound_webhook_received",
        resource_type="activity",
        resource_id=activity["id"],
        details={"event_type": "activity.create", "agent_id": agent_id},
        ip_address=ip_address,
    )
    return {"activity_id": activity["id"], "status": activity["status"]}


def _handle_assign(payload: dict, ip_address: Optional[str]) -> dict:
    activity_id = payload["activity_id"]
    new_agent_id = payload.get("assigned_agent_id")
    if not new_agent_id:
        raise ValueError("activity.assign requires assigned_agent_id")

    activity = activity_service.reassign_activity(activity_id, new_agent_id)
    if not activity:
        raise ValueError(f"Activity not found or not assignable: {activity_id}")

    memory_scope = payload.get("memory_scope")
    if memory_scope:
        activity_service.update_activity(activity_id, memory_scope=memory_scope)

    audit_service.write_event(
        actor_type="system",
        actor_id="inbound-webhook",
        action="inbound_webhook_received",
        resource_type="activity",
        resource_id=activity_id,
        details={"event_type": "activity.assign", "new_agent_id": new_agent_id},
        ip_address=ip_address,
    )
    return {"activity_id": activity_id, "assigned_agent_id": new_agent_id}


def _handle_update(payload: dict, ip_address: Optional[str]) -> dict:
    activity_id = payload["activity_id"]
    updates = {
        k: payload[k]
        for k in ("status", "task_description", "task_result", "memory_scope")
        if k in payload
    }
    if not updates:
        raise ValueError("activity.update requires at least one of: status, task_description, memory_scope")

    updated = activity_service.update_activity(activity_id, **updates)
    if updated is False:
        raise ValueError(f"Activity not found or not updatable: {activity_id}")

    audit_service.write_event(
        actor_type="system",
        actor_id="inbound-webhook",
        action="inbound_webhook_received",
        resource_type="activity",
        resource_id=activity_id,
        details={"event_type": "activity.update", "fields": list(updates.keys())},
        ip_address=ip_address,
    )
    return {"activity_id": activity_id, "updated_fields": list(updates.keys())}


def _handle_cancel(payload: dict, ip_address: Optional[str]) -> dict:
    activity_id = payload["activity_id"]
    reason = payload.get("reason")

    cancelled = activity_service.cancel_activity(activity_id)
    if cancelled is False:
        raise ValueError(f"Activity not found or not cancellable: {activity_id}")

    audit_service.write_event(
        actor_type="system",
        actor_id="inbound-webhook",
        action="inbound_webhook_received",
        resource_type="activity",
        resource_id=activity_id,
        details={"event_type": "activity.cancel", "reason": reason},
        ip_address=ip_address,
    )
    return {"activity_id": activity_id, "status": "cancelled"}


def _handle_note(payload: dict, ip_address: Optional[str]) -> dict:
    activity_id = payload["activity_id"]
    note = payload.get("note", "").strip()
    if not note:
        raise ValueError("activity.note requires a non-empty note")
    audit_service.write_event(
        actor_type="system",
        actor_id="inbound-webhook",
        action="inbound_webhook_note",
        resource_type="activity",
        resource_id=activity_id,
        details={"event_type": "activity.note", "note": note},
        ip_address=ip_address,
    )
    return {"activity_id": activity_id, "noted": True}
