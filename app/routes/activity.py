from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import activity_service, audit_service, briefing_service
from app.services.event_stream_service import event_hub
from app.services import webhook_service
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
    task_note: Optional[str] = None
    task_result: Optional[str] = None
    metadata_json: Optional[str] = None


class RecoveryRequest(BaseModel):
    action: str
    new_agent_id: Optional[str] = None
    task_result: Optional[str] = None


def _can_modify_activity(ctx: RequestContext, activity: dict) -> bool:
    if ctx.is_admin:
        return True
    if ctx.actor_type == "agent":
        return activity.get("assigned_agent_id") == ctx.agent_id
    return activity.get("agent_id") == ctx.agent_id and ctx.actor_type == "user"


def _activity_audit_details(activity: dict, **extra) -> dict:
    details = {
        "activity_id": activity.get("id"),
        "task_description": activity.get("task_description"),
        "memory_scope": activity.get("memory_scope"),
        "agent_id": activity.get("agent_id"),
    }
    if activity.get("task_result") is not None:
        details["task_result"] = activity.get("task_result")
    if activity.get("task_note") is not None:
        details["task_note"] = activity.get("task_note")
    assigned_agent_id = activity.get("assigned_agent_id")
    if assigned_agent_id:
        details["assigned_agent_id"] = assigned_agent_id
    details.update({k: v for k, v in extra.items() if v is not None})
    return details


def _activity_event_data(activity: dict, **extra) -> dict:
    event_data = {
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
    event_data.update({k: v for k, v in extra.items() if v is not None})
    return event_data


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
        details=_activity_audit_details(
            activity,
            new_status=activity["status"],
            action="create",
        ),
    )
    _event_data = _activity_event_data(activity)
    event_hub.publish("activity_created", _event_data)
    webhook_service.dispatch_event("activity_created", _event_data)

    return success_response({"activity": activity}, status_code=201)


@router.post("/pickup")
async def pickup_activity(
    ctx: RequestContext = Depends(get_request_context),
):
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
            details=_activity_audit_details(activity, action="pickup"),
        )

    return success_response(
        {
            "activity": activity,
            "message": None if activity else "No assigned work found for this agent in authorized scopes",
        }
    )


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
        details=_activity_audit_details(
            updated,
            previous_status=activity["status"],
            new_status=body.status or activity["status"],
            action="update",
        ),
    )
    _event_data = _activity_event_data(updated, previous_status=activity["status"])
    event_hub.publish("activity_updated", _event_data)
    webhook_service.dispatch_event("activity_updated", _event_data)

    return success_response({"activity": updated})


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
        details=_activity_audit_details(
            activity,
            action="heartbeat",
            current_status=activity["status"],
        ),
    )
    _event_data = _activity_event_data(activity)
    event_hub.publish("activity_heartbeat", _event_data)
    webhook_service.dispatch_event("activity_heartbeat", _event_data)

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

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    if ctx.actor_type == "agent":
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

    return success_response(
        {
            "activities": activities,
            "total": len(activities),
        }
    )


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
        details=_activity_audit_details(
            activity,
            action="cancel",
            previous_status=activity["status"],
        ),
    )
    updated = activity_service.get_activity(activity_id) or activity
    _event_data = _activity_event_data(updated, previous_status=activity.get("status"))
    event_hub.publish("activity_cancelled", _event_data)
    webhook_service.dispatch_event("activity_cancelled", _event_data)

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
        briefing = briefing_service.generate_handoff_briefing(
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
    recovered = activity_service.get_activity(activity_id) or activity
    _event_data = _activity_event_data(recovered, recovery_action=body.action, result=result_data)
    event_hub.publish("activity_recovered", _event_data)
    webhook_service.dispatch_event("activity_recovered", _event_data)

    return success_response(result_data)
