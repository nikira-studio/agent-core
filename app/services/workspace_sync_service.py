import json
import secrets
from datetime import timedelta
from typing import Optional

from app.config import settings
from app.database import get_db
from app.time_utils import utc_now, utc_now_iso


def record_change(conn, *, memory_scope: str, change_type: str, resource_type: str,
                  resource_id: str, summary: dict, source_agent_id: Optional[str] = None,
                  source_execution_id: Optional[str] = None) -> None:
    if not memory_scope.startswith("workspace:"):
        return
    conn.execute(
        """INSERT INTO workspace_changes
        (id, memory_scope, sequence, change_type, resource_type, resource_id,
         source_agent_id, source_execution_id, summary_json, created_at)
        VALUES (?, ?, COALESCE((SELECT MAX(sequence) + 1 FROM workspace_changes
        WHERE memory_scope = ?), 1), ?, ?, ?, ?, ?, ?, ?)""",
        (secrets.token_urlsafe(16), memory_scope, memory_scope, change_type,
         resource_type, resource_id, source_agent_id, source_execution_id,
         json.dumps(summary, separators=(",", ":")), utc_now_iso()),
    )


def _execution(conn, execution_id: str, agent_id: str, user_id: str, scope: str):
    row = conn.execute(
        "SELECT * FROM agent_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if not row:
        raise ValueError("EXECUTION_NOT_FOUND")
    item = dict(row)
    if item["agent_id"] != agent_id or item["user_id"] != user_id:
        raise PermissionError("EXECUTION_OWNERSHIP")
    if item["memory_scope"] != scope:
        raise ValueError("EXECUTION_SCOPE_MISMATCH")
    return item


def _new_execution(conn, agent_id: str, user_id: str, scope: str, host_session_ref=None):
    execution_id = secrets.token_urlsafe(16)
    now = utc_now_iso()
    conn.execute(
        """INSERT INTO agent_executions
        (id, agent_id, user_id, memory_scope, host_session_ref, status, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (execution_id, agent_id, user_id, scope, host_session_ref, now, now),
    )
    cutoff = (utc_now() - timedelta(hours=settings.WORKSPACE_SYNC_BOOTSTRAP_HOURS)).isoformat()
    row = conn.execute(
        "SELECT MIN(sequence) AS seq FROM workspace_changes WHERE memory_scope = ? AND created_at >= ?",
        (scope, cutoff),
    ).fetchone()
    acknowledged = max(0, int(row["seq"] or 1) - 1)
    conn.execute(
        """INSERT INTO execution_sync_state
        (execution_id, memory_scope, acknowledged_sequence, highest_delivered_sequence, updated_at)
        VALUES (?, ?, ?, ?, ?)""",
        (execution_id, scope, acknowledged, acknowledged, now),
    )
    return execution_id, acknowledged, True


def validate_execution(*, execution_id: Optional[str], agent_id: str, user_id: str, memory_scope: str) -> None:
    if not execution_id:
        return
    with get_db() as conn:
        _execution(conn, execution_id, agent_id, user_id, memory_scope)


def sync_workspace(
    *, agent_id: str, user_id: str, memory_scope: str,
    execution_id: Optional[str] = None, after_cursor: Optional[int] = None,
    limit: int = 100, host_session_ref: Optional[str] = None,
) -> dict:
    limit = min(max(int(limit), 1), 200)
    now = utc_now_iso()
    with get_db() as conn:
        if execution_id:
            _execution(conn, execution_id, agent_id, user_id, memory_scope)
            created = False
            state = conn.execute(
                "SELECT * FROM execution_sync_state WHERE execution_id = ? AND memory_scope = ?",
                (execution_id, memory_scope),
            ).fetchone()
            acknowledged = int(state["acknowledged_sequence"] if state else 0)
        else:
            execution_id, acknowledged, created = _new_execution(
                conn, agent_id, user_id, memory_scope, host_session_ref
            )
        oldest_row = conn.execute(
            "SELECT MIN(sequence) AS seq FROM workspace_changes WHERE memory_scope = ?",
            (memory_scope,),
        ).fetchone()
        oldest_sequence = int(oldest_row["seq"] or 0)
        cursor_reset = bool(oldest_sequence and acknowledged < oldest_sequence - 1)
        if cursor_reset:
            acknowledged = oldest_sequence - 1
            conn.execute(
                """UPDATE execution_sync_state SET acknowledged_sequence = ?,
                highest_delivered_sequence = MAX(highest_delivered_sequence, ?), updated_at = ?
                WHERE execution_id = ? AND memory_scope = ?""",
                (acknowledged, acknowledged, now, execution_id, memory_scope),
            )
        start = acknowledged if after_cursor is None else max(int(after_cursor), acknowledged)
        rows = conn.execute(
            """SELECT * FROM workspace_changes
            WHERE memory_scope = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
            (memory_scope, start, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        changes = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json"))
            except (TypeError, ValueError):
                item["summary"] = {}
                item.pop("summary_json", None)
            changes.append(item)
        next_cursor = int(changes[-1]["sequence"]) if changes else start
        conn.execute(
            "UPDATE agent_executions SET status = 'active', last_seen_at = ? WHERE id = ?",
            (now, execution_id),
        )
        conn.execute(
            """UPDATE execution_sync_state
            SET highest_delivered_sequence = MAX(highest_delivered_sequence, ?), updated_at = ?
            WHERE execution_id = ? AND memory_scope = ?""",
            (next_cursor, now, execution_id, memory_scope),
        )

        pinned = [dict(r) for r in conn.execute(
            """SELECT id, content, memory_class, topic, subject_anchor, record_status
            FROM memory_records WHERE scope = ? AND pinned = 1 AND record_status = 'active'
            ORDER BY created_at""", (memory_scope,)
        ).fetchall()]
        assigned = [dict(r) for r in conn.execute(
            """SELECT id, task_description, task_note, task_result, status,
                      assigned_agent_id, started_at, updated_at
            FROM agent_activity WHERE memory_scope = ? AND assigned_agent_id = ?
              AND status IN ('active', 'stale') ORDER BY started_at""",
            (memory_scope, agent_id),
        ).fetchall()]

    groups = {"memory_changes": [], "activity_changes": [], "briefing_changes": [], "other_session_changes": []}
    for change in changes:
        if change["resource_type"] == "memory":
            groups["memory_changes"].append(change)
        elif change["resource_type"] == "briefing":
            groups["briefing_changes"].append(change)
        else:
            groups["activity_changes"].append(change)
        if change.get("source_execution_id") and change["source_execution_id"] != execution_id:
            groups["other_session_changes"].append(change)
    return {
        "execution_id": execution_id, "execution_created": created,
        "from_cursor": start, "next_cursor": next_cursor, "has_more": has_more,
        "cursor_reset": cursor_reset,
        "cursor_reset_reason": "cursor_expired" if cursor_reset else None,
        "pinned": pinned, "assigned_activities": assigned, **groups,
    }


def acknowledge(*, agent_id: str, user_id: str, execution_id: str, memory_scope: str, cursor: int) -> dict:
    now = utc_now_iso()
    with get_db() as conn:
        _execution(conn, execution_id, agent_id, user_id, memory_scope)
        state = conn.execute(
            "SELECT * FROM execution_sync_state WHERE execution_id = ? AND memory_scope = ?",
            (execution_id, memory_scope),
        ).fetchone()
        if not state:
            raise ValueError("SYNC_STATE_NOT_FOUND")
        cursor = int(cursor)
        if cursor > int(state["highest_delivered_sequence"]):
            raise ValueError("CURSOR_NOT_DELIVERED")
        acknowledged = max(int(state["acknowledged_sequence"]), cursor)
        conn.execute(
            """UPDATE execution_sync_state SET acknowledged_sequence = ?, updated_at = ?
            WHERE execution_id = ? AND memory_scope = ?""",
            (acknowledged, now, execution_id, memory_scope),
        )
        conn.execute(
            "UPDATE agent_executions SET status = 'active', last_seen_at = ? WHERE id = ?",
            (now, execution_id),
        )
    return {"execution_id": execution_id, "acknowledged_cursor": acknowledged}


def end_execution(*, agent_id: str, user_id: str, execution_id: str) -> dict:
    now = utc_now_iso()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_executions WHERE id = ?", (execution_id,)).fetchone()
        if not row:
            raise ValueError("EXECUTION_NOT_FOUND")
        item = dict(row)
        if item["agent_id"] != agent_id or item["user_id"] != user_id:
            raise PermissionError("EXECUTION_OWNERSHIP")
        conn.execute(
            "UPDATE agent_executions SET status = 'ended', ended_at = ?, last_seen_at = ? WHERE id = ?",
            (now, now, execution_id),
        )
    return {"execution_id": execution_id, "status": "ended"}


def list_executions(limit: int = 100, user_id: Optional[str] = None) -> list[dict]:
    with get_db() as conn:
        where = "WHERE e.user_id = ?" if user_id else ""
        params = (user_id, limit) if user_id else (limit,)
        rows = conn.execute(
            """SELECT e.*, s.acknowledged_sequence, s.highest_delivered_sequence,
            MAX(0, s.highest_delivered_sequence - s.acknowledged_sequence) AS unacknowledged
            FROM agent_executions e LEFT JOIN execution_sync_state s ON s.execution_id = e.id
            """ + where + " ORDER BY e.last_seen_at DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def run_maintenance() -> dict:
    stale_cutoff = (utc_now() - timedelta(minutes=settings.EXECUTION_STALE_MINUTES)).isoformat()
    change_cutoff = (utc_now() - timedelta(days=settings.WORKSPACE_CHANGE_RETENTION_DAYS)).isoformat()
    with get_db() as conn:
        stale = conn.execute(
            "UPDATE agent_executions SET status = 'stale' WHERE status = 'active' AND last_seen_at < ?",
            (stale_cutoff,),
        ).rowcount
        pruned = conn.execute(
            "DELETE FROM workspace_changes WHERE created_at < ?", (change_cutoff,)
        ).rowcount
    return {"stale_executions_marked": stale, "workspace_changes_pruned": pruned}
