from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.services import briefing_service, audit_service, agent_service
from app.services.auth_service import get_user_by_id
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer
from app.security.response_helpers import success_response, error_response


router = APIRouter(prefix="/api/briefings", tags=["briefings"])


class HandoffBriefingRequest(BaseModel):
    activity_id: str


class PrdHandoffRequest(BaseModel):
    from_agent_id: str
    to_agent_id: str
    user_id: str


def _briefing_authorized(ctx: RequestContext, activity: dict) -> bool:
    if ctx.is_admin:
        return True
    if ctx.actor_type == "agent":
        return activity.get("assigned_agent_id") == ctx.agent_id
    if ctx.actor_type == "user":
        return activity.get("user_id") == ctx.user_id
    return False


@router.post("/handoff")
async def create_handoff_briefing(
    body: HandoffBriefingRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.agent_id and not ctx.user_id:
        return error_response("CTX_REQUIRED", "Briefing requires agent or user context", 400)

    from app.services import activity_service
    activity = activity_service.get_activity(body.activity_id)
    if not activity:
        return error_response("NOT_FOUND", "Activity not found", 404)

    if not _briefing_authorized(ctx, activity):
        return error_response("FORBIDDEN", "Access denied to this activity", 403)

    memory_scope = activity.get("memory_scope")
    if memory_scope:
        enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id, is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)
        if not enforcer.can_read(memory_scope):
            return error_response("SCOPE_DENIED", "Access denied to activity memory scope", 403)

    briefing = briefing_service.generate_handoff_briefing(
        activity_id=body.activity_id,
        requesting_agent_id=ctx.agent_id or "",
        requesting_user_id=ctx.user_id or "",
        authorized_scopes=ctx.read_scopes,
    )
    if not briefing:
        return error_response("BRIEFING_FAILED", "Could not generate briefing", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="handoff_created",
        resource_type="briefing",
        resource_id=briefing["id"],
        result="success",
        details={"source_activity_id": body.activity_id},
    )

    return success_response({"briefing": briefing}, status_code=201)


@router.post("/handoff/prd")
async def create_prd_handoff_briefing(
    body: PrdHandoffRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.agent_id and not ctx.user_id:
        return error_response("CTX_REQUIRED", "Briefing requires agent or user context", 400)

    enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id or "", is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)
    from_scope = f"agent:{body.from_agent_id}"
    user_scope = f"user:{body.user_id}"

    if not get_user_by_id(body.user_id):
        return error_response("NOT_FOUND", "User not found", 404)
    if not agent_service.get_agent_by_id(body.from_agent_id):
        return error_response("NOT_FOUND", "from_agent not found", 404)
    if not agent_service.get_agent_by_id(body.to_agent_id):
        return error_response("NOT_FOUND", "to_agent not found", 404)

    if ctx.actor_type == "agent" and ctx.agent_id != body.from_agent_id:
        return error_response("FORBIDDEN", "Agents may only create PRD handoffs from their own identity", 403)
    if not ctx.is_admin and ctx.user_id != body.user_id:
        return error_response("FORBIDDEN", "Access denied to requested user", 403)

    if not ctx.is_admin and not enforcer.can_read(from_scope):
        return error_response("SCOPE_DENIED", "Access denied to from_agent scope", 403)
    if not ctx.is_admin and not enforcer.can_read(user_scope):
        return error_response("SCOPE_DENIED", "Access denied to requested user scope", 403)

    briefing = briefing_service.generate_prd_handoff_briefing(
        from_agent_id=body.from_agent_id,
        to_agent_id=body.to_agent_id,
        user_id=body.user_id,
        authorized_scopes=ctx.read_scopes if not ctx.is_admin else [from_scope, user_scope],
        is_admin=ctx.is_admin,
    )
    if not briefing:
        return error_response("BRIEFING_FAILED", "Could not generate briefing", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="briefing_generated",
        resource_type="briefing",
        resource_id=briefing["id"],
        result="success",
        details={"from_agent": body.from_agent_id, "to_agent": body.to_agent_id},
    )

    return success_response({"briefing": briefing}, status_code=201)


@router.get("/{briefing_id}")
async def get_briefing(
    briefing_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    from app.services import activity_service
    activity = activity_service.get_activity(briefing_id)
    if not activity:
        return error_response("NOT_FOUND", "Briefing not found", 404)

    if not _briefing_authorized(ctx, activity):
        return error_response("FORBIDDEN", "Access denied", 403)

    briefing = briefing_service.get_briefing(briefing_id)
    if not briefing:
        return error_response("NOT_FOUND", "Briefing not found", 404)

    return success_response({"briefing": briefing})
