from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Optional

from app.services import activity_service, audit_service
from app.security.dependencies import get_request_context, require_capability
from app.security.effective_authority import EffectiveAuthority
from app.security.scope_enforcer import ScopeEnforcer
from app.security.response_helpers import success_response, error_response
from app.models.enums import ACTIVITY_STATUSES


router = APIRouter(prefix="/api/activity", tags=["activity"], dependencies=[Depends(require_capability("coordination"))])


class CreateActivityRequest(BaseModel):
    task_description: str
    memory_scope: Optional[str] = None
    metadata_json: Optional[str] = None
    task_ref: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    execution_id: Optional[str] = None


class UpdateActivityRequest(BaseModel):
    status: Optional[str] = None
    task_note: Optional[str] = None
    task_result: Optional[str] = None
    metadata_json: Optional[str] = None
    task_ref: Optional[str] = None
    execution_id: Optional[str] = None
    handoff: Optional[dict] = None


class RecoveryRequest(BaseModel):
    action: str
    new_agent_id: Optional[str] = None
    task_result: Optional[str] = None


def _can_activity(ctx: EffectiveAuthority, activity: dict, operation: str) -> bool:
    if ctx.is_admin:
        return True
    if ctx.actor_type == "agent":
        return activity.get("assigned_agent_id") == ctx.agent_id
    return activity.get("agent_id") == ctx.agent_id and ctx.actor_type == "user"


def _enforcer(ctx: EffectiveAuthority, agent_id: Optional[str] = None) -> ScopeEnforcer:
    return ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        agent_id or ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )


def _metadata_with_task_ref(metadata_json: Optional[str], task_ref: Optional[str]) -> Optional[str]:
    """Raises ValueError for a task_ref that breaks the '<system>:<id>' convention."""
    metadata_json = activity_service.validate_metadata_json(metadata_json)
    if task_ref is not None:
        return activity_service.with_task_ref(metadata_json, task_ref)
    return metadata_json



@router.post("")
def create_activity(
    body: CreateActivityRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
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
    try:
        metadata_json = _metadata_with_task_ref(body.metadata_json, body.task_ref)
    except ValueError as exc:
        return error_response("INVALID_TASK_REF", str(exc), 400)
    enforcer = _enforcer(ctx, effective_agent_id)
    if not ctx.is_admin and not enforcer.can_write(memory_scope):
        return error_response(
            "SCOPE_DENIED", f"Access denied to memory_scope: {memory_scope}", 403
        )
    if body.execution_id and ctx.agent_id:
        from app.services import workspace_sync_service
        try:
            workspace_sync_service.validate_execution(
                execution_id=body.execution_id, agent_id=ctx.agent_id,
                user_id=ctx.user_id or "", memory_scope=memory_scope,
            )
        except PermissionError:
            return error_response("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403)
        except ValueError as exc:
            return error_response(str(exc), "Invalid execution", 400)

    activity = activity_service.create_activity(
        agent_id=effective_agent_id,
        user_id=ctx.user_id or "",
        task_description=body.task_description,
        memory_scope=memory_scope,
        metadata_json=metadata_json,
        source_execution_id=body.execution_id,
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
    authorized_scopes = (
        None
        if ctx.is_admin
        else enforcer.filter_readable_scopes(ctx.read_scopes)
    )
    activities = activity_service.search_activities(
        query,
        user_id=ctx.user_id if ctx.actor_type == "user" and not ctx.is_admin else None,
        agent_id=agent_id,
        status=status,
        memory_scope=memory_scope,
        since=since,
        authorized_scopes=authorized_scopes,
        limit=capped_limit,
        offset=safe_offset,
    )

    return success_response({"activities": activities, "total": len(activities)})


@router.get("/attention")
def attention_list(
    scope: Optional[list[str]] = Query(None),
    without_task_ref: bool = False,
    limit: int = 50,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    """Unresolved needs_human handoffs in the scopes the caller reads.

    For a coordinator (an attention reader, a chief-of-staff agent) that has
    to tell a person what is waiting on them. Read-only: resolving stays with
    a person. Repeat `scope` to narrow the list; each must be readable.
    """
    authorized_scopes, denied = _attention_scopes(ctx, scope)
    if denied:
        return error_response("SCOPE_DENIED", f"Access denied to scope: {denied}", 403)
    items = activity_service.list_needs_human(
        authorized_scopes=authorized_scopes,
        without_task_ref=without_task_ref,
        limit=min(max(limit, 0), 200),
    )
    return success_response({"items": items, "count": len(items)})


def _attention_scopes(ctx: EffectiveAuthority, requested: Optional[list[str]]):
    """(scopes to list, first requested scope the caller cannot read)."""
    enforcer = _enforcer(ctx)
    if requested:
        denied = next((s for s in requested if not enforcer.can_read(s)), None)
        return list(dict.fromkeys(requested)), denied
    return (None if ctx.is_admin else enforcer.filter_readable_scopes(ctx.read_scopes)), None


def _can_read_activity(ctx: EffectiveAuthority, activity: dict) -> bool:
    """Whoever can list an activity can open it: its scope is readable."""
    return _can_activity(ctx, activity, "read") or _enforcer(ctx).can_read(
        activity_service.activity_scope(activity)
    )


@router.get("/{activity_id}")
def get_activity(
    activity_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _can_read_activity(ctx, activity):
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
    if body.execution_id and ctx.agent_id:
        from app.services import workspace_sync_service
        try:
            workspace_sync_service.validate_execution(
                execution_id=body.execution_id, agent_id=ctx.agent_id,
                user_id=ctx.user_id or "", memory_scope=activity.get("memory_scope") or f"agent:{ctx.agent_id}",
            )
        except PermissionError:
            return error_response("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403)
        except ValueError as exc:
            return error_response(str(exc), "Invalid execution", 400)

    if body.status and body.status not in ACTIVITY_STATUSES:
        return error_response("INVALID_STATUS", f"Invalid status: {body.status}", 400)
    try:
        metadata_json = _metadata_with_task_ref(
            body.metadata_json if body.metadata_json is not None else (
                activity.get("metadata_json") if body.task_ref is not None else None
            ),
            body.task_ref,
        )
    except ValueError as exc:
        return error_response("INVALID_TASK_REF", str(exc), 400)
    handoff = None
    if body.handoff is not None:
        from app.services.session_context_service import normalize_handoff

        try:
            handoff = normalize_handoff(body.handoff)
        except ValueError as exc:
            return error_response("INVALID_HANDOFF", str(exc), 400)

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
        metadata_json=metadata_json,
        source_execution_id=body.execution_id,
    )
    if not success and handoff is None:
        return error_response("UPDATE_FAILED", "Activity update failed", 500)
    if handoff:
        activity_service.set_handoff(activity_id, handoff)

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


@router.post("/{activity_id}/handoff/resolve")
def resolve_handoff(activity_id: str, ctx: EffectiveAuthority = Depends(get_request_context)):
    """A person marks a handoff's needs_human request as dealt with."""
    if ctx.actor_type != "user":
        return error_response("FORBIDDEN", "Only a person can resolve a handoff request", 403)
    activity = activity_service.get_activity(activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)
    scope = activity.get("memory_scope") or ""
    if not (ctx.is_admin or ctx.can("activity", "update", scope=scope)):
        return error_response("FORBIDDEN", "Access denied", 403)
    if not activity_service.resolve_handoff(activity_id):
        return error_response("NOT_PENDING", "No unresolved handoff on this activity", 409)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="handoff_resolved",
        resource_type="activity",
        resource_id=activity_id,
        result="success",
    )
    resolved = activity_service.get_activity(activity_id) or activity
    activity_service.notify("activity_handoff_resolved", resolved)
    return success_response({"activity": resolved})


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

        result_data["status"] = "reassigned"
        result_data["assigned_agent_id"] = body.new_agent_id
        result_data["reassigned_from"] = updated.get("reassigned_from_agent_id")

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
