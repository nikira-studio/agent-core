import re
import secrets
from typing import Optional
from datetime import timedelta

from app.database import get_db
from app.config import settings
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso


def create_activity(
    agent_id: str,
    user_id: str,
    task_description: str,
    memory_scope: Optional[str] = None,
    metadata_json: Optional[str] = None,
) -> dict:
    activity_id = secrets.token_urlsafe(16)
    now = utc_now_iso()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_activity
            (id, agent_id, assigned_agent_id, user_id, task_description, status, memory_scope,
             started_at, heartbeat_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (activity_id, agent_id, agent_id, user_id, task_description, memory_scope,
             now, now, metadata_json),
        )
        conn.commit()

        return {
            "id": activity_id,
            "agent_id": agent_id,
            "assigned_agent_id": agent_id,
            "user_id": user_id,
            "task_description": task_description,
            "task_note": None,
            "task_result": None,
            "status": "active",
            "memory_scope": memory_scope,
            "started_at": now,
            "heartbeat_at": now,
            "metadata_json": metadata_json,
        }


def get_activity(activity_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity WHERE id = ?
            """,
            (activity_id,),
        ).fetchone()
        return dict(row) if row else None


def heartbeat_activity(activity_id: str) -> bool:
    now = utc_now_iso()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agent_activity SET heartbeat_at = ?, updated_at = ? WHERE id = ? AND status = 'active'",
            (now, now, activity_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_activity(
    activity_id: str,
    task_description: Optional[str] = None,
    task_note: Optional[str] = None,
    task_result: Optional[str] = None,
    memory_scope: Optional[str] = None,
    status: Optional[str] = None,
    metadata_json: Optional[str] = None,
) -> bool:
    updates = []
    params = []
    now = utc_now_iso()

    if task_description is not None:
        updates.append("task_description = ?")
        params.append(task_description)
    if task_note is not None:
        updates.append("task_note = ?")
        params.append(task_note)
    if task_result is not None:
        updates.append("task_result = ?")
        params.append(task_result)
    if memory_scope is not None:
        updates.append("memory_scope = ?")
        params.append(memory_scope)
    if status:
        updates.append("status = ?")
        params.append(status)
        if status in ("completed", "cancelled", "blocked"):
            updates.append("ended_at = ?")
            params.append(now)
    if metadata_json is not None:
        updates.append("metadata_json = ?")
        params.append(metadata_json)

    if not updates:
        return False

    updates.append("updated_at = ?")
    params.append(now)
    params.append(activity_id)

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE agent_activity SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def list_activities(
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conditions = ["1=1"]
    params = []

    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assigned_agent_id:
        conditions.append("assigned_agent_id = ?")
        params.append(assigned_agent_id)

    where = " AND ".join(conditions)
    params.extend([limit, offset])

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity
            WHERE {where}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


ACTIVITY_COLUMNS = (
    "id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id, "
    "task_description, task_note, task_result, status, memory_scope, started_at, "
    "updated_at, heartbeat_at, ended_at, metadata_json"
)


def _sanitize_fts_query(query: str) -> str:
    """Quote each token so user text can never be read as FTS5 operator syntax.

    Mirrors memory_service._sanitize_fts_query: tokens are AND-ed, and anything
    that is not alphanumeric is dropped rather than escaped, so a query like
    `NEAR("a" "b")` or an unbalanced quote degrades to a plain term search
    instead of raising fts5: syntax error.
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
    return " ".join(f'"{token}"' for token in tokens)


def search_activities(
    query: str,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    memory_scope: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Full-text search the activity trail — the episodic "what did we work on".

    Ordered newest-first rather than by FTS rank: the question this answers is
    almost always "what happened recently on X", and a three-month-old task that
    happens to repeat a keyword is rarely the better answer. Callers that want
    relevance can narrow with memory_scope/agent_id instead.

    Returns [] for a query with no searchable tokens rather than falling back to
    listing everything, so an empty result is never mistaken for "no matches".
    """
    sanitized = _sanitize_fts_query(query)
    if not sanitized:
        return []

    conditions = ["agent_activity_fts MATCH ?"]
    params: list = [sanitized]

    if user_id:
        conditions.append("a.user_id = ?")
        params.append(user_id)
    if agent_id:
        conditions.append("(a.agent_id = ? OR a.assigned_agent_id = ?)")
        params.extend([agent_id, agent_id])
    if status:
        conditions.append("a.status = ?")
        params.append(status)
    if memory_scope:
        conditions.append("a.memory_scope = ?")
        params.append(memory_scope)
    if since:
        conditions.append("datetime(a.started_at) >= datetime(?)")
        params.append(since)

    where = " AND ".join(conditions)
    params.extend([max(limit, 0), max(offset, 0)])

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT a.{ACTIVITY_COLUMNS.replace(', ', ', a.')}
            FROM agent_activity a
            JOIN agent_activity_fts fts ON fts.rowid = a.rowid
            WHERE {where}
            ORDER BY a.started_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def count_activities(
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
) -> int:
    conditions = ["1=1"]
    params = []
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assigned_agent_id:
        conditions.append("assigned_agent_id = ?")
        params.append(assigned_agent_id)

    where = " AND ".join(conditions)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM agent_activity WHERE {where}", params
        ).fetchone()
        return row[0] if row else 0


def mark_stale_activities(threshold_minutes: Optional[int] = None) -> int:
    if threshold_minutes is None:
        threshold_minutes = settings.STALE_THRESHOLD_MINUTES
    cutoff = utc_now() - timedelta(minutes=threshold_minutes)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, heartbeat_at FROM agent_activity WHERE status = 'active'"
        ).fetchall()
        stale_ids = [
            row["id"]
            for row in rows
            if row["heartbeat_at"] and parse_utc_datetime(row["heartbeat_at"]) < cutoff
        ]
        if not stale_ids:
            return 0
        cursor = conn.execute(
            f"UPDATE agent_activity SET status = 'stale', updated_at = ? WHERE id IN ({','.join('?' for _ in stale_ids)})",
            [utc_now_iso(), *stale_ids],
        )
        conn.commit()
        return cursor.rowcount


def reassign_activity(
    activity_id: str,
    new_agent_id: str,
) -> Optional[dict]:
    activity = get_activity(activity_id)
    if not activity:
        return None
    if activity["status"] not in ("active", "stale"):
        return None

    old_agent = activity["assigned_agent_id"] or activity["agent_id"]
    now = utc_now_iso()

    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE agent_activity
            SET assigned_agent_id = ?, reassigned_from_agent_id = ?,
                status = 'active', heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('active', 'stale')
            """,
            (new_agent_id, old_agent, now, now, activity_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None

    return get_activity(activity_id)


def cancel_activity(activity_id: str) -> bool:
    now = utc_now_iso()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agent_activity SET status = 'cancelled', ended_at = ?, updated_at = ? WHERE id = ?",
            (now, now, activity_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def claim_next_activity(
    agent_id: str,
    authorized_scopes: list[str],
) -> Optional[dict]:
    if not authorized_scopes:
        return None
    now = utc_now_iso()
    placeholders = ",".join("?" for _ in authorized_scopes)
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity
            WHERE assigned_agent_id = ? AND status = 'active'
            AND memory_scope IN ({placeholders})
            ORDER BY started_at ASC
            LIMIT 1
            """,
            [agent_id] + list(authorized_scopes),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE agent_activity SET heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'active' AND assigned_agent_id = ?
            """,
            (now, now, row["id"], agent_id),
        )
        result = dict(row)
        result["heartbeat_at"] = now
        result["updated_at"] = now
        return result


def get_active_activity_for_agent(agent_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    conditions = ["assigned_agent_id = ? AND status IN ('active', 'stale')"]
    params = [agent_id]
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)

    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None


def event_data(activity: dict, **extra) -> dict:
    """Activity payload for webhooks and the event stream.

    Lives here rather than in each transport: it was duplicated line-for-line in
    the REST and MCP routers, which is how two copies of the same payload drift.
    """
    payload = {
        "activity_id": activity.get("id"),
        "task_description": activity.get("task_description"),
        "task_note": activity.get("task_note"),
        "task_result": activity.get("task_result"),
        "agent_id": activity.get("agent_id"),
        "assigned_agent_id": activity.get("assigned_agent_id"),
        "user_id": activity.get("user_id"),
        "memory_scope": activity.get("memory_scope"),
        "status": activity.get("status"),
        "started_at": activity.get("started_at"),
        "updated_at": activity.get("updated_at"),
        "heartbeat_at": activity.get("heartbeat_at"),
        "ended_at": activity.get("ended_at"),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def notify(event: str, activity: dict, **extra) -> dict:
    """Announce an activity change to everything that watches for one.

    There are two listeners — the dashboard's live event stream and any
    registered webhooks — and both must hear about a change regardless of which
    transport made it. Sharing the payload was not enough: MCP built the same
    `event_data` and then dispatched only webhooks, so an agent working over
    MCP (the path agents actually use) never moved the live dashboard. Delivery
    belongs next to the payload for the same reason the payload was pulled out.
    """
    from app.services import webhook_service
    from app.services.event_stream_service import event_hub

    payload = event_data(activity, **extra)
    event_hub.publish(event, payload)
    webhook_service.dispatch_event(event, payload)
    return payload


def audit_details(activity: dict, **extra) -> dict:
    """Audit detail for an activity change."""
    details = {
        "activity_id": activity.get("id"),
        "task_description": activity.get("task_description"),
        "memory_scope": activity.get("memory_scope"),
        "agent_id": activity.get("agent_id"),
    }
    for field in ("task_result", "task_note"):
        if activity.get(field) is not None:
            details[field] = activity.get(field)
    if activity.get("assigned_agent_id"):
        details["assigned_agent_id"] = activity.get("assigned_agent_id")
    details.update({k: v for k, v in extra.items() if v is not None})
    return details
