import json
import secrets
from datetime import timedelta
from typing import Optional

from app.config import settings
from app.database import get_db
from app.time_utils import utc_now, utc_now_iso


def record_change(
    conn,
    *,
    memory_scope: str,
    change_type: str,
    resource_type: str,
    resource_id: str,
    summary: dict,
    source_agent_id: Optional[str] = None,
    source_execution_id: Optional[str] = None,
) -> None:
    if not memory_scope.startswith("workspace:"):
        return
    conn.execute(
        """INSERT INTO workspace_changes
        (id, memory_scope, sequence, change_type, resource_type, resource_id,
         source_agent_id, source_execution_id, summary_json, created_at)
        VALUES (?, ?, COALESCE((SELECT MAX(sequence) + 1 FROM workspace_changes
        WHERE memory_scope = ?), 1), ?, ?, ?, ?, ?, ?, ?)""",
        (
            secrets.token_urlsafe(16),
            memory_scope,
            memory_scope,
            change_type,
            resource_type,
            resource_id,
            source_agent_id,
            source_execution_id,
            json.dumps(summary, separators=(",", ":")),
            utc_now_iso(),
        ),
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


def _new_execution(
    conn, agent_id: str, user_id: str, scope: str, host_session_ref=None
):
    execution_id = secrets.token_urlsafe(16)
    now = utc_now_iso()
    conn.execute(
        """INSERT INTO agent_executions
        (id, agent_id, user_id, memory_scope, host_session_ref, status, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (execution_id, agent_id, user_id, scope, host_session_ref, now, now),
    )
    cutoff = (
        utc_now() - timedelta(hours=settings.WORKSPACE_SYNC_BOOTSTRAP_HOURS)
    ).isoformat()
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


def validate_execution(
    *, execution_id: Optional[str], agent_id: str, user_id: str, memory_scope: str
) -> None:
    if not execution_id:
        return
    with get_db() as conn:
        _execution(conn, execution_id, agent_id, user_id, memory_scope)


def sync_workspace(
    *,
    agent_id: str,
    user_id: str,
    memory_scope: str,
    execution_id: Optional[str] = None,
    after_cursor: Optional[int] = None,
    limit: int = 100,
    host_session_ref: Optional[str] = None,
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
        start = (
            acknowledged
            if after_cursor is None
            else max(int(after_cursor), acknowledged)
        )
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

        pinned = [
            dict(r)
            for r in conn.execute(
                """SELECT id, content, memory_class, topic, subject_anchor, record_status
            FROM memory_records WHERE scope = ? AND pinned = 1 AND record_status = 'active'
            ORDER BY created_at""",
                (memory_scope,),
            ).fetchall()
        ]
        assigned = [
            dict(r)
            for r in conn.execute(
                """SELECT id, task_description, task_note, task_result, status,
                      assigned_agent_id, started_at, updated_at
            FROM agent_activity WHERE memory_scope = ? AND assigned_agent_id = ?
              AND status IN ('active', 'stale') ORDER BY started_at""",
                (memory_scope, agent_id),
            ).fetchall()
        ]

    groups = {
        "memory_changes": [],
        "activity_changes": [],
        "briefing_changes": [],
        "other_session_changes": [],
    }
    for change in changes:
        if change["resource_type"] == "memory":
            groups["memory_changes"].append(change)
        elif change["resource_type"] == "briefing":
            groups["briefing_changes"].append(change)
        else:
            groups["activity_changes"].append(change)
        if (
            change.get("source_execution_id")
            and change["source_execution_id"] != execution_id
        ):
            groups["other_session_changes"].append(change)
    return {
        "execution_id": execution_id,
        "execution_created": created,
        "from_cursor": start,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "cursor_reset": cursor_reset,
        "cursor_reset_reason": "cursor_expired" if cursor_reset else None,
        "pinned": pinned,
        "assigned_activities": assigned,
        **groups,
    }


def acknowledge(
    *, agent_id: str, user_id: str, execution_id: str, memory_scope: str, cursor: int
) -> dict:
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
        row = conn.execute(
            "SELECT * FROM agent_executions WHERE id = ?", (execution_id,)
        ).fetchone()
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
            """
            + where
            + " ORDER BY e.last_seen_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def run_maintenance() -> dict:
    stale_cutoff = (
        utc_now() - timedelta(minutes=settings.EXECUTION_STALE_MINUTES)
    ).isoformat()
    change_cutoff = (
        utc_now() - timedelta(days=settings.WORKSPACE_CHANGE_RETENTION_DAYS)
    ).isoformat()
    with get_db() as conn:
        stale = conn.execute(
            "UPDATE agent_executions SET status = 'stale' WHERE status = 'active' AND last_seen_at < ?",
            (stale_cutoff,),
        ).rowcount
        pruned = conn.execute(
            "DELETE FROM workspace_changes WHERE created_at < ?", (change_cutoff,)
        ).rowcount
    return {"stale_executions_marked": stale, "workspace_changes_pruned": pruned}


def digest_for_new_arrival(
    memory_scope: str,
    prior_activity_id: Optional[str],
    new_activity_id: str,
    limit: int = 10,
) -> dict:
    """Build a "since you were last active" digest for an agent starting fresh.

    Returns a dict with `baseline` ("prior_activity" or "retained_tail"),
    `changes` (the actual rows), and three boundary fields so the caller
    always knows exactly what the digest is and is not looking at:
    `total_available`, `truncated`, `oldest_available_at`.

    Sequence-based cutoff, no timestamp comparison. The cutoff sequence is
    `MAX(sequence) FROM workspace_changes WHERE resource_type='activity'
    AND resource_id=<prior_activity id>` — the exact point up to which the
    prior activity's own existence is already reflected in the feed. A
    non-NULL result is itself proof the cutoff row is still in the retained
    table; a NULL result (prior activity never existed, or its rows have
    been pruned) falls back to `baseline="retained_tail"` with cutoff 0.

    The new activity's own `workspace_changes` row (stamped at creation by
    `workspace_activity_ai`) is excluded so the digest never includes the
    call's own side effect.

    `total_available` and the returned rows are computed from one query
    using `COUNT(*) OVER()` — a second query for the count could disagree
    with the rows if a concurrent write lands between them.
    """
    if not memory_scope or not memory_scope.startswith("workspace:"):
        return {
            "baseline": "retained_tail",
            "changes": [],
            "total_available": 0,
            "truncated": False,
            "oldest_available_at": None,
        }

    # Clamp the limit to a sane bound. Caller validates it from the system
    # settings already, but defensive here too.
    limit = max(1, min(int(limit), 50))

    with get_db() as conn:
        if prior_activity_id:
            cutoff_row = conn.execute(
                "SELECT MAX(sequence) AS s FROM workspace_changes "
                "WHERE memory_scope = ? AND resource_type = 'activity' "
                "AND resource_id = ?",
                (memory_scope, prior_activity_id),
            ).fetchone()
            cutoff = (
                cutoff_row["s"] if cutoff_row and cutoff_row["s"] is not None else 0
            )
            baseline = (
                "prior_activity"
                if cutoff_row and cutoff_row["s"] is not None
                else "retained_tail"
            )
        else:
            cutoff = 0
            baseline = "retained_tail"

        # Fetch `limit + 1` rows so we can detect truncation without a
        # second count query. COUNT(*) OVER() gives total_available from the
        # same snapshot.
        rows = conn.execute(
            """
            SELECT id, memory_scope, sequence, change_type, resource_type,
                   resource_id, source_agent_id, source_execution_id,
                   summary_json, created_at,
                   COUNT(*) OVER() AS total_matching
            FROM workspace_changes
            WHERE memory_scope = ?
              AND sequence > ?
              AND NOT (resource_type = 'activity' AND resource_id = ?)
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (memory_scope, cutoff, new_activity_id, limit + 1),
        ).fetchall()

        truncated = len(rows) > limit
        rows = rows[:limit]
        total_available = int(rows[0]["total_matching"]) if rows else 0

        # The scope's overall retained boundary, reported regardless of
        # baseline. Different question (how far back could *any* answer
        # reach?) from the digest's own result set, so a separate query
        # is unavoidable and correct here.
        oldest_row = conn.execute(
            "SELECT MIN(created_at) AS at FROM workspace_changes WHERE memory_scope = ?",
            (memory_scope,),
        ).fetchone()
        oldest_available_at = oldest_row["at"] if oldest_row else None

    changes = []
    for row in rows:
        item = dict(row)
        item.pop("total_matching", None)
        try:
            item["summary"] = json.loads(item.pop("summary_json"))
        except (TypeError, ValueError):
            item["summary"] = {}
        changes.append(item)

    return {
        "baseline": baseline,
        "changes": changes,
        "total_available": total_available,
        "truncated": truncated,
        "oldest_available_at": oldest_available_at,
    }


def execution_is_caught_up(
    execution_id: str,
    memory_scope: str,
    *,
    exclude_activity_id: str,
) -> bool:
    """True iff `execution_id`'s highest delivered sequence covers everything
    that exists in `memory_scope` other than the activity we're about to attach.

    Used only by the digest-suppression check in `activity_update`. Activity
    creation fires the `workspace_activity_ai` trigger, which inserts a new
    `workspace_changes` row stamped for the new activity. Without excluding
    that row from the comparison, an execution that was genuinely caught up
    the instant before this very call would immediately test as behind — the
    call's own side effect moved the goalpost it's being measured against.
    The new activity is not something any execution could have "delivered"
    before it existed.

    `exclude_activity_id` is required, not optional, because that race is
    structural, not a misconfiguration to guard against.

    A scope with no other changes at all is trivially caught up. An
    execution with no sync state (e.g., just-created but never delivered
    anything yet) is not caught up — its `highest_delivered_sequence` is 0.
    """
    if not execution_id or not memory_scope:
        return False
    with get_db() as conn:
        state = conn.execute(
            "SELECT highest_delivered_sequence FROM execution_sync_state "
            "WHERE execution_id = ? AND memory_scope = ?",
            (execution_id, memory_scope),
        ).fetchone()
        highest = int(state["highest_delivered_sequence"]) if state else 0

        # MAX(sequence) excluding the new activity's own change row. If the
        # scope has no other changes, this returns NULL and we treat the
        # scope as trivially caught up.
        max_row = conn.execute(
            "SELECT MAX(sequence) AS s FROM workspace_changes "
            "WHERE memory_scope = ? AND NOT ("
            "    resource_type = 'activity' AND resource_id = ?"
            ")",
            (memory_scope, exclude_activity_id),
        ).fetchone()
        scope_max = int(max_row["s"]) if max_row and max_row["s"] is not None else 0

    return highest >= scope_max
