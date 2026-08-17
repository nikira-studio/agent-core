import asyncio
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import activity_service, audit_service, briefing_service
from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.scope_enforcer import ScopeEnforcer
from app.security.response_helpers import success_response, error_response
from app.models.enums import ACTIVITY_STATUSES


router = APIRouter(prefix="/api/activity", tags=["activity"])


class CreateActivityRequest(BaseModel):
    task_description: str
    memory_scope: Optional[str] = None
    metadata_json: Optional[str] = None
    assigned_agent_id: Optional[str] = None


class UpdateActivityRequest(BaseModel):
    status: Optional[str] = None
    task_note: Optional[str] = None
    task_result: Optional[str] = None
    metadata_json: Optional[str] = None


class RecoveryRequest(BaseModel):
    action: str
    new_agent_id: Optional[str] = None
    task_result: Optional[str] = None


def _can_activity(ctx: EffectiveAuthority, activity: dict, operation: str) -> bool:
    if ctx.is_delegated:
        return ctx.can_resource("activity", operation, activity["id"])
    if ctx.is_admin:
        return True
    if ctx.actor_type == "agent":
        return activity.get("assigned_agent_id") == ctx.agent_id
    return activity.get("agent_id") == ctx.agent_id and ctx.actor_type == "user"



@router.post("")
def create_activity(
    body: CreateActivityRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    if ctx.is_delegated:
        return error_response("FORBIDDEN", "Delegated activity creation is unsupported", 403)
    if ctx.agent_id:
        effective_agent_id = ctx.agent_id
    elif ctx.is_admin and body.assigned_agent_id:
        effective_agent_id = body.assigned_agent_id
    else:
        return error_response(
            "AGENT_REQUIRED", "Activity requires an agent context", 400
        )

    memory_scope = body.memory_scope or f"agent:{effective_agent_id}"
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id or effective_agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not ctx.is_admin and not enforcer.can_write(memory_scope):
        return error_response(
            "SCOPE_DENIED", f"Access denied to memory_scope: {memory_scope}", 403
        )

    activity = activity_service.create_activity(
        agent_id=effective_agent_id,
        user_id=ctx.user_id or "",
        task_description=body.task_description,
        memory_scope=memory_scope,
        metadata_json=body.metadata_json,
    )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_update",
        resource_type="activity",
        resource_id=activity["id"],
        result="success",
        details=activity_service.audit_details(
            activity,
            new_status=activity["status"],
            action="create",
        ),
    )
    activity_service.notify("activity_created", activity)

    return success_response({"activity": activity}, status_code=201)


@router.post("/pickup")
def pickup_activity(
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    if ctx.is_delegated:
        return error_response("FORBIDDEN", "Delegated activity pickup is unsupported", 403)
    if not ctx.agent_id:
        return error_response("AGENT_REQUIRED", "Pickup requires an agent context", 400)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    authorized_scopes = enforcer.filter_readable_scopes(ctx.read_scopes)

    activity = activity_service.claim_next_activity(ctx.agent_id, authorized_scopes)

    if activity:
        audit_service.write_event(
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            action="activity_pickup",
            resource_type="activity",
            resource_id=activity["id"],
            result="success",
            details=activity_service.audit_details(activity, action="pickup"),
        )

    return success_response(
        {
            "activity": activity,
            "message": None if activity else "No assigned work found for this agent in authorized scopes",
        }
    )


# Declared before /{activity_id} so "search" is not captured as an activity id.
@router.get("/search")
def search_activities(
    query: str,
    agent_id: Optional[str] = None,
    memory_scope: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    """Full-text search the activity trail.

    Visibility is scope-based rather than caller-locked (unlike GET /api/activity,
    which narrows an agent caller to its own rows): the point of searching the
    trail is that one agent can find what another already did in a workspace they
    both read. Rows outside the caller's readable scopes are dropped.
    """
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    capped_limit = min(max(limit, 0), 100)
    safe_offset = max(offset, 0)
    fetch_limit = min(max((capped_limit + safe_offset) * 3, 50), 300)

    raw_activities = activity_service.search_activities(
        query,
        user_id=ctx.user_id if ctx.actor_type == "user" and not ctx.is_admin else None,
        agent_id=agent_id,
        status=status,
        memory_scope=memory_scope,
        since=since,
        limit=fetch_limit,
        offset=0,
    )

    activities = []
    for activity in raw_activities:
        scope = activity.get("memory_scope") or f"agent:{activity['agent_id']}"
        if ctx.is_delegated:
            if not ctx.can_resource("activity", "read", activity["id"]):
                continue
        elif not ctx.is_admin and not enforcer.can_read(scope):
            continue
        activities.append(activity)

    activities = activities[safe_offset : safe_offset + capped_limit]

    return success_response({"activities": activities, "total": len(activities)})


@router.get("/{activity_id}")
def get_activity(
    activity_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_activity(ctx, activity, "read"):
        return error_response("FORBIDDEN", "Access denied", 403)

    return success_response({"activity": activity})


@router.put("/{activity_id}")
def update_activity(
    activity_id: str,
    body: UpdateActivityRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_activity(ctx, activity, "update"):
        return error_response("FORBIDDEN", "Access denied", 403)

    if body.status and body.status not in ACTIVITY_STATUSES:
        return error_response("INVALID_STATUS", f"Invalid status: {body.status}", 400)

    if body.status in ("completed", "cancelled", "blocked") and activity[
        "status"
    ] not in ("active", "stale"):
        return error_response(
            "INVALID_TRANSITION", "Cannot close a non-active activity", 400
        )

    success = activity_service.update_activity(
        activity_id,
        status=body.status,
        task_note=body.task_note,
        task_result=body.task_result,
        metadata_json=body.metadata_json,
    )
    if not success:
        return error_response("UPDATE_FAILED", "Activity update failed", 500)

    updated = activity_service.get_activity(activity_id) or activity
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_update",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
        details=activity_service.audit_details(
            updated,
            previous_status=activity["status"],
            new_status=body.status or activity["status"],
            action="update",
        ),
    )
    activity_service.notify(
        "activity_updated", updated, previous_status=activity["status"]
    )

    return success_response({"activity": updated})


@router.post("/{activity_id}/heartbeat")
def heartbeat_activity(
    activity_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_activity(ctx, activity, "update"):
        return error_response("FORBIDDEN", "Access denied", 403)

    if activity["status"] not in ("active", "stale"):
        return error_response(
            "INVALID_STATUS", "Cannot heartbeat a non-active activity", 400
        )

    success = activity_service.heartbeat_activity(activity_id)
    if not success:
        return error_response("HEARTBEAT_FAILED", "Heartbeat failed", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_heartbeat",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
        details=activity_service.audit_details(
            activity,
            action="heartbeat",
            current_status=activity["status"],
        ),
    )
    activity_service.notify("activity_heartbeat", activity)

    return success_response({"activity": activity_service.get_activity(activity_id)})


@router.get("")
def list_activities(
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity_service.mark_stale_activities()

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    if ctx.is_delegated:
        filter_agent_id = None
    elif ctx.actor_type == "agent":
        filter_agent_id = ctx.agent_id
    else:
        filter_agent_id = agent_id

    if filter_agent_id and not enforcer.can_read(f"agent:{filter_agent_id}"):
        return error_response(
            "FORBIDDEN", "Access denied to this agent's activities", 403
        )

    activities = activity_service.list_activities(
        user_id=ctx.user_id if ctx.actor_type == "user" else None,
        agent_id=filter_agent_id,
        status=status,
        limit=min(limit, 100),
        offset=offset,
    )
    if ctx.is_delegated:
        activities = [
            activity for activity in activities
            if ctx.can_resource("activity", "read", activity["id"])
        ]

    return success_response(
        {
            "activities": activities,
            "total": len(activities),
        }
    )


@router.delete("/{activity_id}")
def cancel_activity(
    activity_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_activity(ctx, activity, "cancel"):
        return error_response("FORBIDDEN", "Access denied", 403)

    if activity["status"] not in ("active", "stale"):
        return error_response(
            "INVALID_STATUS", "Cannot cancel a non-active activity", 400
        )

    success = activity_service.cancel_activity(activity_id)
    if not success:
        return error_response("CANCEL_FAILED", "Cancel failed", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_cancelled",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
        details=activity_service.audit_details(
            activity,
            action="cancel",
            previous_status=activity["status"],
        ),
    )
    updated = activity_service.get_activity(activity_id) or activity
    activity_service.notify(
        "activity_cancelled", updated, previous_status=activity.get("status")
    )

    return success_response({"message": "Activity cancelled"})


@router.post("/{activity_id}/recovery")
async def recover_activity(
    activity_id: str,
    body: RecoveryRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required for recovery", 403)

    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    valid_actions = (
        "mark_completed",
        "mark_cancelled",
        "resume_with_same_agent",
        "create_handoff_briefing",
        "reassign_to_agent",
    )
    if body.action not in valid_actions:
        return error_response(
            "INVALID_ACTION", f"Recovery action must be one of {valid_actions}", 400
        )

    result_data = {"activity_id": activity_id}

    if body.action == "mark_completed":
        activity_service.update_activity(
            activity_id,
            status="completed",
            task_result=body.task_result,
        )
        result_data["status"] = "completed"
        if body.task_result is not None:
            result_data["task_result"] = body.task_result

    elif body.action == "mark_cancelled":
        activity_service.cancel_activity(activity_id)
        result_data["status"] = "cancelled"

    elif body.action == "resume_with_same_agent":
        if activity["status"] != "stale":
            return error_response(
                "INVALID_STATUS", "Can only resume stale activities", 400
            )
        activity_service.update_activity(activity_id, status="active")
        activity_service.heartbeat_activity(activity_id)
        result_data["status"] = "active"

    elif body.action == "create_handoff_briefing":
        activity_for_briefing = activity_service.get_activity(activity_id)
        requesting_agent = (
            ctx.agent_id
            if ctx.agent_id
            else (
                activity_for_briefing["assigned_agent_id"]
                if activity_for_briefing
                else None
            )
            or (activity_for_briefing["agent_id"] if activity_for_briefing else None)
            or ""
        )
        briefing = await asyncio.to_thread(
            briefing_service.generate_handoff_briefing,
            activity_id=activity_id,
            requesting_agent_id=requesting_agent,
            requesting_user_id=ctx.user_id or "",
        )
        if not briefing:
            return error_response("BRIEFING_FAILED", "Could not generate briefing", 500)
        result_data["briefing_id"] = briefing["id"]
        result_data["status"] = "reassigned"

    elif body.action == "reassign_to_agent":
        if not body.new_agent_id:
            return error_response(
                "NEW_AGENT_REQUIRED",
                "new_agent_id is required for reassign_to_agent",
                400,
            )

        updated = activity_service.reassign_activity(activity_id, body.new_agent_id)
        if not updated:
            return error_response("REASSIGN_FAILED", "Could not reassign activity", 500)

        briefing = await asyncio.to_thread(
            briefing_service.generate_handoff_briefing,
            activity_id=activity_id,
            requesting_agent_id=body.new_agent_id,
            requesting_user_id=ctx.user_id or "",
        )

        result_data["status"] = "reassigned"
        result_data["assigned_agent_id"] = body.new_agent_id
        result_data["reassigned_from"] = updated.get("reassigned_from_agent_id")
        if briefing:
            result_data["briefing_id"] = briefing["id"]

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_recovery",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
        details={"action": body.action, "result": result_data},
    )
    recovered = activity_service.get_activity(activity_id) or activity
    activity_service.notify(
        "activity_recovered", recovered, recovery_action=body.action, result=result_data
    )

    return success_response(result_data)
