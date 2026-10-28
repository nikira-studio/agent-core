import json
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
    source_execution_id: Optional[str] = None,
) -> dict:
    activity_id = secrets.token_urlsafe(16)
    now = utc_now_iso()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_activity
            (id, agent_id, assigned_agent_id, user_id, task_description, status, memory_scope,
             started_at, heartbeat_at, metadata_json, source_execution_id)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                activity_id,
                agent_id,
                agent_id,
                user_id,
                task_description,
                memory_scope,
                now,
                now,
                metadata_json,
                source_execution_id,
            ),
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
                   heartbeat_at, ended_at, metadata_json, handoff_json, handoff_requested_at, handoff_resolved_at
            FROM agent_activity WHERE id = ?
            """,
            (activity_id,),
        ).fetchone()
        return dict(row) if row else None


# Statuses whose needs_human request counts as open: the same ones session
# context and the Activity page list them for.
OPEN_REQUEST_STATUSES = ("blocked", "completed", "active", "stale")
HANDOFF_RESOLUTION_KINDS = ("person", "agent", "superseded")
REQUEST_SUMMARY_CHARS = 200


def load_handoff(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def split_asks(text: Optional[str]) -> list[str]:
    """The separate asks in a needs_human text: its blank-line paragraphs."""
    return [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]


def merge_handoff(previous: dict, new: dict, *, previous_resolved: bool) -> dict:
    """An amendment to a closed task's handoff: it adds, it does not replace.

    needs_human stays one string (every reader treats it as text); each ask
    is a paragraph, and the amendment's asks are appended as new paragraphs
    unless the same paragraph is already there. A request that was already
    resolved is no longer an ask, so new asks start a fresh request. The
    next_steps and open_questions lists gain the new items they lack;
    completed is replaced when the amendment gives one. Nothing is removed:
    an ask leaves only through activity_handoff_resolve or a person.
    """
    from app.services.session_context_service import HANDOFF_MAX_CHARS

    merged = {k: v for k, v in previous.items() if k != "resolution"}
    if new.get("completed"):
        merged["completed"] = new["completed"]
    for key in ("next_steps", "open_questions"):
        items = list(merged.get(key) or [])
        items += [item for item in new.get(key) or [] if item not in items]
        if items:
            merged[key] = items
    if new.get("needs_human"):
        asks = [] if previous_resolved else split_asks(previous.get("needs_human"))
        asks += [ask for ask in split_asks(new["needs_human"]) if ask not in asks]
        merged["needs_human"] = "\n\n".join(asks)
    if len(json.dumps(merged)) > HANDOFF_MAX_CHARS:
        raise ValueError(
            f"the amended handoff would be over {HANDOFF_MAX_CHARS} characters; "
            "resolve asks that are done with activity_handoff_resolve first"
        )
    return merged


def set_handoff(activity_id: str, handoff: dict, *, amend: bool = False) -> Optional[dict]:
    """Store a validated structured handoff (see session_context_service).

    With amend=True the handoff is merged into the stored one (merge_handoff)
    instead of replacing it; that is how a closed task's handoff is changed.

    A request already resolved stays resolved unless the new handoff asks a
    person for something different; when it stays resolved, the recorded
    resolution is carried over. The caller's handoff never carries a
    resolution of its own: normalize_handoff rejects the field.

    handoff_requested_at is set to now whenever a needs_human request is
    recorded or changes. Before a handoff exists, the column is the Stop
    hook's "already asked" stamp; once one exists nothing reads it for that,
    so it becomes the time the request was made, which superseding compares
    against session start. Returns the stored handoff, or None if there is
    no such activity. Raises ValueError when an amendment is too large.
    """
    now = utc_now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT handoff_json, handoff_resolved_at, handoff_requested_at FROM agent_activity WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if not row:
            return None
        previous = load_handoff(row["handoff_json"]) if row["handoff_json"] is not None else {}
        was_resolved = bool(row["handoff_resolved_at"]) and row["handoff_json"] is not None
        if amend:
            stored = merge_handoff(previous, handoff, previous_resolved=was_resolved)
        else:
            stored = {k: v for k, v in handoff.items() if k != "resolution"}
        same_request = previous.get("needs_human") == stored.get("needs_human")
        resolved_at = None
        if was_resolved and same_request:
            resolved_at = row["handoff_resolved_at"]
            if isinstance(previous.get("resolution"), dict):
                stored["resolution"] = previous["resolution"]
        requested_at = row["handoff_requested_at"]
        if stored.get("needs_human") and not same_request:
            requested_at = now
        conn.execute(
            "UPDATE agent_activity SET handoff_json = ?, handoff_resolved_at = ?, "
            "handoff_requested_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(stored, separators=(",", ":")), resolved_at, requested_at, now, activity_id),
        )
        conn.commit()
        return stored


def _resolve_in_connection(
    conn,
    activity_id: str,
    *,
    kind: str,
    resolved_by: Optional[str],
    reason: Optional[str] = None,
    superseded_by: Optional[str] = None,
    at: Optional[str] = None,
) -> Optional[dict]:
    if kind not in HANDOFF_RESOLUTION_KINDS:
        raise ValueError(f"unknown resolution kind: {kind}")
    at = at or utc_now_iso()
    resolution = {"kind": kind, "at": at, "resolved_by": resolved_by}
    if reason:
        resolution["reason"] = reason
    if superseded_by:
        resolution["superseded_by"] = superseded_by
    cursor = conn.execute(
        "UPDATE agent_activity SET handoff_resolved_at = ?, "
        "handoff_json = json_set(handoff_json, '$.resolution', json(?)) "
        "WHERE id = ? AND json_valid(handoff_json) "
        "AND COALESCE(json_extract(handoff_json, '$.needs_human'), '') != '' "
        "AND handoff_resolved_at IS NULL",
        (at, json.dumps(resolution, separators=(",", ":")), activity_id),
    )
    return resolution if cursor.rowcount > 0 else None


def resolve_handoff(
    activity_id: str,
    *,
    kind: str = "person",
    resolved_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[dict]:
    """Mark a handoff's needs_human request as dealt with.

    kind is who settled it: "person" (the Activity page) or "agent" (the agent
    that asked, through activity_handoff_resolve). The resolution is stored in
    handoff_json under "resolution" beside handoff_resolved_at. Returns the
    resolution, or None when there was no open request to resolve.
    """
    with get_db() as conn:
        resolution = _resolve_in_connection(
            conn, activity_id, kind=kind, resolved_by=resolved_by, reason=reason
        )
        conn.commit()
        return resolution


def request_summary(row: dict) -> dict:
    """An open or superseded request as an agent sees it: enough to restate it."""
    handoff = load_handoff(row.get("handoff_json"))
    description = " ".join((row.get("task_description") or "").split())
    if len(description) > REQUEST_SUMMARY_CHARS:
        description = description[: REQUEST_SUMMARY_CHARS - 1].rstrip() + "…"
    summary = {
        "activity_id": row["id"],
        "summary": description,
        "status": row.get("status"),
        "needs_human": handoff.get("needs_human"),
        "requested_at": row.get("updated_at"),
    }
    if handoff.get("open_questions"):
        summary["open_questions"] = handoff["open_questions"]
    return summary


def _open_requests_query(conn, *, agent_id: str, user_id: Optional[str], memory_scope: str,
                         exclude_id: Optional[str]) -> list[dict]:
    conditions = [
        "assigned_agent_id = ?",
        "memory_scope = ?",
        "json_valid(handoff_json)",
        "COALESCE(json_extract(handoff_json, '$.needs_human'), '') != ''",
        "handoff_resolved_at IS NULL",
        f"status IN ({', '.join('?' for _ in OPEN_REQUEST_STATUSES)})",
    ]
    params: list = [agent_id, memory_scope, *OPEN_REQUEST_STATUSES]
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if exclude_id:
        conditions.append("id != ?")
        params.append(exclude_id)
    rows = conn.execute(
        "SELECT id, agent_id, assigned_agent_id, user_id, memory_scope, task_description, "
        "status, updated_at, ended_at, handoff_requested_at, handoff_json, metadata_json "
        f"FROM agent_activity WHERE {' AND '.join(conditions)} ORDER BY updated_at DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _request_time(row: dict):
    """When a request was made. handoff_requested_at is stamped when one is
    recorded; legacy rows (from before that) may lack it, and then the end of
    the task, or failing that its last update, stands in. A legacy row whose
    column holds the Stop hook's earlier stamp reads as slightly older than
    it is, which only matters for a session that overlapped it."""
    for value in (row.get("handoff_requested_at"), row.get("ended_at"), row.get("updated_at")):
        if value:
            try:
                return parse_utc_datetime(value)
            except (TypeError, ValueError):
                continue
    return None


def task_ref(activity: dict) -> Optional[str]:
    """The caller's own task identifier, metadata.task_ref (for example
    "foundry:<objective_id>"), or None when the activity has none."""
    metadata = load_handoff(activity.get("metadata_json"))  # any JSON object
    value = metadata.get("task_ref")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _same_task(rows: list[dict], ref: Optional[str]) -> list[dict]:
    """Requests that belong to the same task stream as `ref`.

    Tools that run several tasks as one agent in one scope (Foundry's
    objectives, OpenAgents specialists) tag each activity with
    metadata.task_ref; one task's request must not replace another's. Two
    activities without a task_ref (ordinary terminal sessions) count as the
    same stream; a task_ref'd one and an untagged one never do.
    """
    return [row for row in rows if task_ref(row) == ref]


def _made_before(rows: list[dict], cutoff: Optional[str]) -> list[dict]:
    """Requests made before `cutoff` (a session's start). A parallel session's
    request made after this one started is that session's business."""
    if not cutoff:
        return rows
    limit = parse_utc_datetime(cutoff)
    kept = []
    for row in rows:
        made = _request_time(row)
        if made is not None and made < limit:
            kept.append(row)
    return kept


def list_open_requests_for_agent(
    agent_id: str,
    memory_scope: str,
    user_id: Optional[str] = None,
    exclude_id: Optional[str] = None,
    made_before: Optional[str] = None,
    same_task_as: Optional[dict] = None,
) -> list[dict]:
    """This agent's own unresolved needs_human requests in one scope, newest
    first; with made_before, only those made before that time; with
    same_task_as, only those with that activity's task_ref."""
    with get_db() as conn:
        rows = _open_requests_query(
            conn, agent_id=agent_id, user_id=user_id, memory_scope=memory_scope, exclude_id=exclude_id
        )
    if same_task_as is not None:
        rows = _same_task(rows, task_ref(same_task_as))
    return _made_before(rows, made_before)


def supersede_open_requests(activity: dict, *, resolved_by: Optional[str]) -> list[dict]:
    """A new needs_human handoff on `activity` replaces the older open requests.

    Superseded: requests of activities with the same assigned agent, the same
    user and the same memory scope, other than `activity` itself, made before
    `activity` started, and with the same metadata.task_ref (both absent
    counts as the same). A request a parallel session made after this one
    started, or one of a different task, is left alone. They are resolved with kind "superseded" pointing
    at `activity`, and returned (with their asks) so the agent can restate any
    that still apply. Nothing crosses a scope or a user.
    """
    agent_id = activity.get("assigned_agent_id")
    scope = activity.get("memory_scope")
    if not agent_id or not scope:
        return []
    superseded = []
    with get_db() as conn:
        rows = _made_before(
            _same_task(
                _open_requests_query(
                    conn,
                    agent_id=agent_id,
                    user_id=activity.get("user_id") or "",
                    memory_scope=scope,
                    exclude_id=activity["id"],
                ),
                task_ref(activity),
            ),
            activity.get("started_at"),
        )
        at = utc_now_iso()
        for row in rows:
            resolution = _resolve_in_connection(
                conn,
                row["id"],
                kind="superseded",
                resolved_by=resolved_by,
                superseded_by=activity["id"],
                at=at,
            )
            if resolution:
                superseded.append({**row, "resolution": resolution})
        conn.commit()
    return superseded


def handoff_followups(
    activity: dict,
    handoff: Optional[dict],
    *,
    closing: bool,
    actor_type: str,
    actor_id: Optional[str],
) -> dict:
    """What an update that carried a handoff, or closed a task, does to the
    assigned agent's other requests. Shared by MCP activity_update and REST
    PUT /api/activity/{id}, so both answer with the same fields.

    - A needs_human request supersedes the agent's earlier open requests in
      the scope (supersede_open_requests): returned as superseded_requests,
      each audited as handoff_superseded and announced.
    - Otherwise, closing (completed or blocked) lists the agent's earlier open
      requests as open_requests with a reminder, and resolves nothing.

    `activity` is the updated activity. Returns response fields to merge:
    superseded_requests or open_requests, and warnings.
    """
    from app.services import audit_service

    extra: dict = {}
    warnings: list[dict] = []
    if closing and not activity.get("handoff_json"):
        warnings.append(
            {
                "code": "HANDOFF_MISSING",
                "message": "Closed without a handoff. Pass handoff={completed, next_steps, "
                "open_questions, needs_human} so the next session can pick this up.",
            }
        )
    scope = activity.get("memory_scope")
    if handoff and handoff.get("needs_human"):
        rows = supersede_open_requests(activity, resolved_by=actor_id)
        for row in rows:
            summary = request_summary(row)
            audit_service.write_event(
                actor_type=actor_type,
                actor_id=actor_id,
                action="handoff_superseded",
                resource_type="activity",
                resource_id=row["id"],
                result="success",
                details=audit_details(
                    row,
                    kind="superseded",
                    actor_type=actor_type,
                    superseded_by=activity["id"],
                    needs_human=summary.get("needs_human"),
                ),
            )
            notify(
                "activity_updated",
                get_activity(row["id"]) or row,
                handoff_resolution="superseded",
                superseded_by=activity["id"],
            )
        if rows:
            extra["superseded_requests"] = [request_summary(row) for row in rows]
            warnings.append(
                {
                    "code": "HANDOFF_REQUESTS_SUPERSEDED",
                    "message": (
                        f"This handoff replaced {len(rows)} earlier open request(s) of this agent in "
                        f"{scope}, listed in superseded_requests; they are no longer waiting on a "
                        "person. If any still applies, restate it now: while the task is open, include "
                        "it in the full handoff; once the task is closed, send a handoff with only the "
                        "restated ask and it is added to the asks already there."
                    ),
                }
            )
    elif closing and scope and activity.get("assigned_agent_id"):
        rows = list_open_requests_for_agent(
            activity["assigned_agent_id"],
            scope,
            user_id=activity.get("user_id") or "",
            exclude_id=activity["id"],
            made_before=activity.get("started_at"),
            same_task_as=activity,
        )
        if rows:
            extra["open_requests"] = [request_summary(row) for row in rows]
            warnings.append(
                {
                    "code": "OPEN_HANDOFF_REQUESTS",
                    "message": (
                        f"This agent has {len(rows)} earlier open request(s) to a person in this scope, "
                        "listed in open_requests: resolve each one that has been dealt with using "
                        "activity_handoff_resolve(activity_id, reason), or restate it in a handoff."
                    ),
                }
            )
    if warnings:
        extra["warnings"] = warnings
    return extra


def list_needs_human(user_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Unresolved handoff requests for a person, newest first.

    Each row carries the request text as `needs_human`. Pass user_id to limit
    the list to that user's activities; None lists every user's.
    """
    conditions = [
        "json_valid(handoff_json)",
        "COALESCE(json_extract(handoff_json, '$.needs_human'), '') != ''",
        "handoff_resolved_at IS NULL",
        # The same statuses session context shows them for.
        "status IN ('blocked', 'completed', 'active', 'stale')",
    ]
    params: list = []
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, agent_id, memory_scope, task_description, updated_at, "
            "json_extract(handoff_json, '$.needs_human') AS needs_human "
            f"FROM agent_activity WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def list_superseded_requests(user_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Requests a later handoff replaced, newest first, for the Activity page.

    They left the waiting queue but stay visible, so a person can see what an
    agent stopped asking for and which handoff replaced it.
    """
    conditions = [
        "json_valid(handoff_json)",
        "handoff_resolved_at IS NOT NULL",
        "json_extract(handoff_json, '$.resolution.kind') = 'superseded'",
    ]
    params: list = []
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, agent_id, memory_scope, task_description, handoff_resolved_at, "
            "json_extract(handoff_json, '$.needs_human') AS needs_human, "
            "json_extract(handoff_json, '$.resolution.superseded_by') AS superseded_by "
            f"FROM agent_activity WHERE {' AND '.join(conditions)} "
            "ORDER BY handoff_resolved_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


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
    source_execution_id: Optional[str] = None,
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
    if source_execution_id is not None:
        updates.append("source_execution_id = ?")
        params.append(source_execution_id)

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
    authorized_scopes: Optional[list[str]] = None,
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
    if authorized_scopes is not None:
        if not authorized_scopes:
            return []
        placeholders = ",".join("?" for _ in authorized_scopes)
        conditions.append(
            f"(memory_scope IN ({placeholders}) OR "
            f"(memory_scope IS NULL AND ('agent:' || agent_id) IN ({placeholders})))"
        )
        params.extend([*authorized_scopes, *authorized_scopes])

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


def list_attention_activities(
    user_id: Optional[str] = None,
    limit: int = 8,
    lookback_days: Optional[int] = None,
) -> list[dict]:
    """Return current unresolved work for the Overview attention queue.

    Activity history is intentionally retained, so a blocked or stale record
    must not remain an alert indefinitely.  A later completed retry of the
    exact same task also resolves the older alert for dashboard purposes while
    preserving both records in the Activity trail.
    """
    if lookback_days is None:
        lookback_days = settings.ATTENTION_LOOKBACK_DAYS
    cutoff = (utc_now() - timedelta(days=max(0, lookback_days))).isoformat()

    conditions = [
        "a.status IN ('stale', 'blocked')",
        "datetime(a.updated_at) >= datetime(?)",
        """NOT EXISTS (
            SELECT 1 FROM agent_activity completed
            WHERE completed.status = 'completed'
              AND completed.user_id = a.user_id
              AND completed.agent_id = a.agent_id
              AND completed.task_description = a.task_description
              AND datetime(completed.started_at) >= datetime(a.started_at)
        )""",
    ]
    params: list = [cutoff]
    if user_id:
        conditions.append("a.user_id = ?")
        params.append(user_id)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT a.{ACTIVITY_COLUMNS.replace(", ", ", a.")}
            FROM agent_activity a
            WHERE {" AND ".join(conditions)}
            ORDER BY datetime(a.updated_at) DESC
            LIMIT ?
            """,
            [*params, max(0, limit)],
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
    authorized_scopes: Optional[list[str]] = None,
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
    if authorized_scopes is not None:
        if not authorized_scopes:
            return []
        placeholders = ",".join("?" for _ in authorized_scopes)
        conditions.append(
            f"(a.memory_scope IN ({placeholders}) OR "
            f"(a.memory_scope IS NULL AND ('agent:' || a.agent_id) IN ({placeholders})))"
        )
        params.extend([*authorized_scopes, *authorized_scopes])

    where = " AND ".join(conditions)
    params.extend([max(limit, 0), max(offset, 0)])

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT a.{ACTIVITY_COLUMNS.replace(", ", ", a.")}
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


def get_active_activity_for_agent(
    agent_id: str,
    user_id: Optional[str] = None,
    memory_scope: Optional[str] = None,
) -> Optional[dict]:
    conditions = ["assigned_agent_id = ? AND status IN ('active', 'stale')"]
    params = [agent_id]
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if memory_scope is not None:
        conditions.append("memory_scope = ?")
        params.append(memory_scope)

    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity
            WHERE {" AND ".join(conditions)}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None


def get_latest_closed_activity_for_agent(
    agent_id: str,
    memory_scope: str,
    user_id: Optional[str] = None,
) -> Optional[dict]:
    """The agent's most recent task in `memory_scope`, if it is closed.

    Lets an agent amend the handoff it just closed with, for example to
    restate a request its new handoff superseded. Only the latest task
    qualifies, and only while it is completed or blocked.
    """
    conditions = ["assigned_agent_id = ?", "memory_scope = ?"]
    params: list = [agent_id, memory_scope]
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT id, status FROM agent_activity WHERE {' AND '.join(conditions)} "
            "ORDER BY started_at DESC LIMIT 1",
            params,
        ).fetchone()
    if not row or row["status"] not in ("completed", "blocked"):
        return None
    return get_activity(row["id"])


def get_last_activity_in_scope(
    agent_id: str,
    memory_scope: str,
    exclude_id: Optional[str] = None,
) -> Optional[dict]:
    """Return the agent's most recent activity in `memory_scope`."""
    conditions = ["agent_id = ?", "memory_scope = ?"]
    params: list = [agent_id, memory_scope]
    if exclude_id:
        conditions.append("id != ?")
        params.append(exclude_id)

    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id, agent_id, user_id, assigned_agent_id, reassigned_from_agent_id,
                   task_description, task_note, task_result, status, memory_scope, started_at, updated_at,
                   heartbeat_at, ended_at, metadata_json
            FROM agent_activity
            WHERE {" AND ".join(conditions)}
            ORDER BY started_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None


def execution_linked_workspace_stats() -> dict:
    """Read-only stat: workspace-scope activities that are linked to an execution.

    Scoped to `memory_scope LIKE 'workspace:%'` — `agent:*`-scoped activities
    are structurally incapable of using `workspace_sync` and must not appear
    in either the numerator or the denominator. Returns both a lifetime and
    a normalized 30-day window, since production shows these tell
    meaningfully different stories and a single blended number would hide
    the trend.

    The 30-day comparison goes through SQLite's `datetime()` on both sides
    (`started_at` is ISO-with-`T` from application code, not the plain
    `CURRENT_TIMESTAMP` format that `datetime('now', '-30 days')` returns,
    so a bare string compare gives a measurably different, wrong count —
    verified directly against production. See planb.md Workstream 2.

    Returns 0/0 on a fresh installation rather than raising on divide-by-zero;
    callers render the fraction.
    """
    with get_db() as conn:
        total_lifetime = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM agent_activity "
                "WHERE memory_scope LIKE 'workspace:%'"
            ).fetchone()["n"]
        )
        linked_lifetime = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM agent_activity "
                "WHERE memory_scope LIKE 'workspace:%' "
                "AND source_execution_id IS NOT NULL "
                "AND source_execution_id != ''"
            ).fetchone()["n"]
        )
        total_30d = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM agent_activity "
                "WHERE memory_scope LIKE 'workspace:%' "
                "AND datetime(started_at) >= datetime('now', '-30 days')"
            ).fetchone()["n"]
        )
        linked_30d = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM agent_activity "
                "WHERE memory_scope LIKE 'workspace:%' "
                "AND source_execution_id IS NOT NULL "
                "AND source_execution_id != '' "
                "AND datetime(started_at) >= datetime('now', '-30 days')"
            ).fetchone()["n"]
        )

    def pct(num, den):
        return round((num / den) * 100, 1) if den else 0.0

    return {
        "workspace_activities_total": total_lifetime,
        "workspace_activities_execution_linked": linked_lifetime,
        "workspace_activities_pct_lifetime": pct(linked_lifetime, total_lifetime),
        "workspace_activities_total_30d": total_30d,
        "workspace_activities_execution_linked_30d": linked_30d,
        "workspace_activities_pct_30d": pct(linked_30d, total_30d),
    }


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
