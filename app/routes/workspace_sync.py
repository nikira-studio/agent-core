from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.response_helpers import error_response, success_response
from app.services import audit_service, workspace_sync_service

router = APIRouter(prefix="/api/workspace-sync", tags=["workspace_sync"])


class SyncRequest(BaseModel):
    memory_scope: str
    execution_id: Optional[str] = None
    after_cursor: Optional[int] = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    host_session_ref: Optional[str] = None


class AckRequest(BaseModel):
    memory_scope: str
    execution_id: str
    cursor: int = Field(ge=0)


class EndRequest(BaseModel):
    execution_id: str


def _agent(ctx: EffectiveAuthority):
    if ctx.is_delegated:
        return None, error_response("FORBIDDEN", "Delegated workspace sync is unsupported", 403)
    if not ctx.agent_id:
        return None, error_response("AGENT_REQUIRED", "Workspace sync requires an agent", 400)
    return ctx.agent_id, None


@router.post("")
def sync(body: SyncRequest, ctx: EffectiveAuthority = Depends(get_request_context)):
    agent_id, error = _agent(ctx)
    if error:
        return error
    if not ctx.can("memory", "read", scope=body.memory_scope):
        return error_response("SCOPE_DENIED", "Access denied to this workspace", 403)
    try:
        result = workspace_sync_service.sync_workspace(
            agent_id=agent_id, user_id=ctx.user_id or "", memory_scope=body.memory_scope,
            execution_id=body.execution_id, after_cursor=body.after_cursor,
            limit=body.limit, host_session_ref=body.host_session_ref,
        )
    except PermissionError:
        return error_response("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403)
    except ValueError as exc:
        return error_response(str(exc), "Invalid execution or cursor", 400)
    return success_response(result)


@router.post("/ack")
def ack(body: AckRequest, ctx: EffectiveAuthority = Depends(get_request_context)):
    agent_id, error = _agent(ctx)
    if error:
        return error
    if not ctx.can("memory", "read", scope=body.memory_scope):
        return error_response("SCOPE_DENIED", "Access denied to this workspace", 403)
    try:
        result = workspace_sync_service.acknowledge(
            agent_id=agent_id, user_id=ctx.user_id or "", execution_id=body.execution_id,
            memory_scope=body.memory_scope, cursor=body.cursor,
        )
    except PermissionError:
        return error_response("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403)
    except ValueError as exc:
        return error_response(str(exc), "Invalid acknowledgement cursor", 400)
    audit_service.write_event(actor_type=ctx.actor_type, actor_id=ctx.actor_id,
        action="workspace_sync_ack", resource_type="execution",
        resource_id=body.execution_id, result="success", details=result)
    return success_response(result)


@router.post("/end")
def end(body: EndRequest, ctx: EffectiveAuthority = Depends(get_request_context)):
    agent_id, error = _agent(ctx)
    if error:
        return error
    try:
        result = workspace_sync_service.end_execution(
            agent_id=agent_id, user_id=ctx.user_id or "", execution_id=body.execution_id
        )
    except PermissionError:
        return error_response("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403)
    except ValueError as exc:
        return error_response(str(exc), "Execution not found", 404)
    audit_service.write_event(actor_type=ctx.actor_type, actor_id=ctx.actor_id,
        action="execution_ended", resource_type="execution",
        resource_id=body.execution_id, result="success", details=result)
    return success_response(result)
