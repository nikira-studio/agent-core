import json
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import activity_service, audit_service, briefing_service
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
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
    metadata_json: Optional[str] = None


class RecoveryRequest(BaseModel):
    action: str
    new_agent_id: Optional[str] = None


def _can_modify_activity(ctx: RequestContext, activity: dict) -> bool:
    if ctx.is_admin:
        return True
    if ctx.actor_type == "agent":
        return activity.get("assigned_agent_id") == ctx.agent_id
    return activity.get("agent_id") == ctx.agent_id and ctx.actor_type == "user"


@router.post("")
async def create_activity(
    body: CreateActivityRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if ctx.agent_id:
        effective_agent_id = ctx.agent_id
    elif ctx.is_admin and body.assigned_agent_id:
        effective_agent_id = body.assigned_agent_id
    else:
        return error_response("AGENT_REQUIRED", "Activity requires an agent context", 400)

    memory_scope = body.memory_scope or f"agent:{effective_agent_id}"
    enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id or effective_agent_id, is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)
    if not ctx.is_admin and not enforcer.can_write(memory_scope):
        return error_response("SCOPE_DENIED", f"Access denied to memory_scope: {memory_scope}", 403)

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
    )

    return success_response({"activity": activity}, status_code=201)


@router.get("/{activity_id}")
async def get_activity(
    activity_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_modify_activity(ctx, activity):
        return error_response("FORBIDDEN", "Access denied", 403)

    return success_response({"activity": activity})


@router.put("/{activity_id}")
async def update_activity(
    activity_id: str,
    body: UpdateActivityRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_modify_activity(ctx, activity):
        return error_response("FORBIDDEN", "Access denied", 403)

    if body.status and body.status not in ACTIVITY_STATUSES:
        return error_response("INVALID_STATUS", f"Invalid status: {body.status}", 400)

    if body.status in ("completed", "cancelled", "blocked") and activity["status"] not in ("active", "stale"):
        return error_response("INVALID_TRANSITION", "Cannot close a non-active activity", 400)

    success = activity_service.update_activity(
        activity_id,
        status=body.status,
        metadata_json=body.metadata_json,
    )
    if not success:
        return error_response("UPDATE_FAILED", "Activity update failed", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="activity_update",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
    )

    return success_response({"activity": activity_service.get_activity(activity_id)})


@router.post("/{activity_id}/heartbeat")
async def heartbeat_activity(
    activity_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_modify_activity(ctx, activity):
        return error_response("FORBIDDEN", "Access denied", 403)

    if activity["status"] not in ("active", "stale"):
        return error_response("INVALID_STATUS", "Cannot heartbeat a non-active activity", 400)

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
    )

    return success_response({"activity": activity_service.get_activity(activity_id)})


@router.get("")
async def list_activities(
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    activity_service.mark_stale_activities()

    enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id, is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)

    if ctx.actor_type == "agent":
        filter_agent_id = ctx.agent_id
    else:
        filter_agent_id = agent_id

    if filter_agent_id and not enforcer.can_read(f"agent:{filter_agent_id}"):
        return error_response("FORBIDDEN", "Access denied to this agent's activities", 403)

    activities = activity_service.list_activities(
        user_id=ctx.user_id if ctx.actor_type == "user" else None,
        agent_id=filter_agent_id,
        status=status,
        limit=min(limit, 100),
        offset=offset,
    )

    return success_response({
        "activities": activities,
        "total": len(activities),
    })


@router.delete("/{activity_id}")
async def cancel_activity(
    activity_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_modify_activity(ctx, activity):
        return error_response("FORBIDDEN", "Access denied", 403)

    if activity["status"] not in ("active", "stale"):
        return error_response("INVALID_STATUS", "Cannot cancel a non-active activity", 400)

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
    )

    return success_response({"message": "Activity cancelled"})


@router.post("/{activity_id}/recovery")
async def recover_activity(
    activity_id: str,
    body: RecoveryRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required for recovery", 403)

    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    valid_actions = ("mark_completed", "mark_cancelled", "resume_with_same_agent",
                     "create_handoff_briefing", "reassign_to_agent")
    if body.action not in valid_actions:
        return error_response("INVALID_ACTION", f"Recovery action must be one of {valid_actions}", 400)

    result_data = {"activity_id": activity_id}

    if body.action == "mark_completed":
        activity_service.update_activity(activity_id, status="completed")
        result_data["status"] = "completed"

    elif body.action == "mark_cancelled":
        activity_service.cancel_activity(activity_id)
        result_data["status"] = "cancelled"

    elif body.action == "resume_with_same_agent":
        if activity["status"] != "stale":
            return error_response("INVALID_STATUS", "Can only resume stale activities", 400)
        activity_service.update_activity(activity_id, status="active")
        activity_service.heartbeat_activity(activity_id)
        result_data["status"] = "active"

    elif body.action == "create_handoff_briefing":
        briefing = briefing_service.generate_handoff_briefing(
            activity_id=activity_id,
            requesting_agent_id=ctx.agent_id or "",
            requesting_user_id=ctx.user_id or "",
        )
        if not briefing:
            return error_response("BRIEFING_FAILED", "Could not generate briefing", 500)
        result_data["briefing_id"] = briefing["id"]
        result_data["status"] = "reassigned"

    elif body.action == "reassign_to_agent":
        if not body.new_agent_id:
            return error_response("NEW_AGENT_REQUIRED", "new_agent_id is required for reassign_to_agent", 400)

        updated = activity_service.reassign_activity(activity_id, body.new_agent_id)
        if not updated:
            return error_response("REASSIGN_FAILED", "Could not reassign activity", 500)

        briefing = briefing_service.generate_handoff_briefing(
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

    return success_response(result_data)
