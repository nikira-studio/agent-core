import json
import secrets
from datetime import timedelta
from typing import Optional

from app.database import get_db
from app.services import memory_service, activity_service, workspace_service
from app.time_utils import utc_now, utc_now_iso


def generate_handoff_briefing(
    activity_id: str,
    requesting_agent_id: str,
    requesting_user_id: str,
    authorized_scopes: Optional[list[str]] = None,
    is_admin: bool = False,
) -> Optional[dict]:
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return None

    briefing_id = secrets.token_urlsafe(16)
    now = utc_now_iso()

    memory_scope = activity.get("memory_scope")
    if not memory_scope:
        memory_scope = activity["agent_id"]

    all_records = memory_service.get_memory_by_scope(
        scope=memory_scope,
        limit=100,
        record_status="active",
    )

    if authorized_scopes:
        from app.security.scope_enforcer import ScopeEnforcer
        workspace_ids = {
            scope.split(":", 1)[1]
            for scope in authorized_scopes
            if scope.startswith("workspace:") and ":" in scope
        }
        enforcer = ScopeEnforcer(
            authorized_scopes,
            authorized_scopes,
            requesting_agent_id,
            is_admin=is_admin,
            active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
        )
        memory_records = [r for r in all_records if enforcer.can_read(r["scope"])]
    else:
        memory_records = all_records

    decisions = [r for r in memory_records if r.get("memory_class") == "decision"]
    facts = [r for r in memory_records if r.get("memory_class") == "fact"]
    preferences = [r for r in memory_records if r.get("memory_class") == "preference"]

    recent_activities = activity_service.list_activities(
        user_id=requesting_user_id,
        agent_id=activity["agent_id"],
        status="completed",
        limit=5,
    )

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_activity
            (id, agent_id, assigned_agent_id, user_id, task_description, status, memory_scope, started_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)
            """,
            (
                briefing_id,
                requesting_agent_id,
                requesting_agent_id,
                requesting_user_id,
                f"Handoff briefing for activity {activity_id}",
                memory_scope,
                now,
                json.dumps({
                    "type": "handoff_briefing",
                    "source_activity_id": activity_id,
                    "decisions_count": len(decisions),
                    "facts_count": len(facts),
                    "preferences_count": len(preferences),
                    "recent_tasks_count": len(recent_activities),
                }),
            ),
        )
        conn.commit()

    briefing = {
        "id": briefing_id,
        "source_activity_id": activity_id,
        "agent_id": activity["agent_id"],
        "assigned_agent_id": activity.get("assigned_agent_id") or activity["agent_id"],
        "task_description": activity["task_description"],
        "started_at": activity["started_at"],
        "decisions": [{"id": r["id"], "content": r["content"]} for r in decisions[:10]],
        "facts": [{"id": r["id"], "content": r["content"]} for r in facts[:10]],
        "preferences": [{"id": r["id"], "content": r["content"]} for r in preferences[:10]],
        "recent_completed": [
            {"id": a["id"], "task_description": a["task_description"], "ended_at": a.get("ended_at")}
            for a in recent_activities
        ],
        "generated_at": now,
    }

    with get_db() as conn:
        conn.execute(
            "UPDATE agent_activity SET metadata_json = ? WHERE id = ?",
            (json.dumps({"briefing": briefing}), briefing_id),
        )
        conn.commit()

    return briefing


def generate_prd_handoff_briefing(
    from_agent_id: str,
    to_agent_id: str,
    user_id: str,
    authorized_scopes: Optional[list[str]] = None,
    is_admin: bool = False,
) -> Optional[dict]:
    now = utc_now_iso()
    briefing_id = secrets.token_urlsafe(16)

    from app.security.scope_enforcer import ScopeEnforcer
    read_scopes = authorized_scopes or [f"agent:{from_agent_id}", f"user:{user_id}"]
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in read_scopes
        if scope.startswith("workspace:") and ":" in scope
    }
    enforcer = ScopeEnforcer(
        read_scopes,
        [],
        from_agent_id,
        is_admin=is_admin,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
    )

    active_task = activity_service.get_active_activity_for_agent(from_agent_id, user_id)

    seven_days_ago = (utc_now() - timedelta(days=7)).isoformat()
    with get_db() as conn:
        decision_rows = conn.execute(
            """
            SELECT id, content, confidence FROM memory_records
            WHERE scope = ? AND memory_class = 'decision'
              AND record_status = 'active' AND created_at >= ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (f"agent:{from_agent_id}", seven_days_ago),
        ).fetchall()

    key_decisions = [
        {"id": r["id"], "content": r["content"], "confidence": r["confidence"]}
        for r in decision_rows
    ]

    active_context_records = memory_service.get_memory_by_scope(
        scope=f"agent:{from_agent_id}",
        limit=20,
        record_status="active",
    ) + memory_service.get_memory_by_scope(
        scope=f"user:{user_id}",
        limit=20,
        record_status="active",
    )

    active_context = [
        {"memory_class": r.get("memory_class"), "content": r["content"]}
        for r in active_context_records
        if enforcer.can_read(r["scope"])
    ]

    recent_completed = activity_service.list_activities(
        user_id=user_id,
        agent_id=from_agent_id,
        status="completed",
        limit=3,
    )

    briefing = {
        "id": briefing_id,
        "generated_at": now,
        "from_agent": from_agent_id,
        "to_agent": to_agent_id,
        "user": user_id,
        "active_task": {
            "id": active_task["id"],
            "description": active_task["task_description"],
            "started_at": active_task["started_at"],
        } if active_task else None,
        "recent_completed": [
            {"description": a["task_description"], "ended_at": a.get("ended_at"), "outcome": "success"}
            for a in recent_completed
        ],
        "key_decisions": key_decisions,
        "active_context": active_context[:20],
    }

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_activity
            (id, agent_id, assigned_agent_id, user_id, task_description, status, memory_scope, started_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)
            """,
            (
                briefing_id,
                from_agent_id,
                to_agent_id,
                user_id,
                f"PRD handoff briefing {from_agent_id} -> {to_agent_id}",
                f"agent:{from_agent_id}",
                now,
                json.dumps({"briefing": briefing}),
            ),
        )
        conn.commit()

    return briefing


def get_briefing(briefing_id: str) -> Optional[dict]:
    activity = activity_service.get_activity(briefing_id)
    if not activity:
        return None
    metadata = activity.get("metadata_json")
    if metadata:
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict) and parsed.get("briefing"):
                return parsed["briefing"]
        except (json.JSONDecodeError, TypeError):
            pass
    return None
