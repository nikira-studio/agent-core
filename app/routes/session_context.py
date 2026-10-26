from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.security.dependencies import get_request_context, require_capability
from app.security.effective_authority import EffectiveAuthority
from app.security.response_helpers import error_response, success_response
from app.services import session_context_service

router = APIRouter(
    prefix="/api/session-context",
    tags=["session_context"],
    dependencies=[Depends(require_capability("coordination"))],
)


class SessionContextRequest(BaseModel):
    memory_scope: Optional[str] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    host_session_ref: Optional[str] = Field(default=None, max_length=200)
    source: Optional[str] = Field(default=None, max_length=40)
    max_chars: int = Field(
        default=session_context_service.DEFAULT_BUDGET_CHARS,
        ge=session_context_service.MIN_BUDGET_CHARS,
        le=session_context_service.MAX_BUDGET_CHARS,
    )


def readable_workspace_scopes(ctx: EffectiveAuthority) -> list[str]:
    # Every readable workspace, not just the default recall set: the session's
    # working directory decides which one applies.
    candidates = list(dict.fromkeys([*ctx.default_recall_scopes, *ctx.read_scopes]))
    return [
        scope
        for scope in ctx.filter_scopes("memory", "read", candidates)
        if scope.startswith("workspace:")
    ]


@router.post("")
def session_context(
    body: SessionContextRequest, ctx: EffectiveAuthority = Depends(get_request_context)
):
    if not ctx.agent_id:
        return error_response("AGENT_REQUIRED", "Session context requires an agent", 400)
    if body.memory_scope and not ctx.can("memory", "read", scope=body.memory_scope):
        return error_response("SCOPE_DENIED", "Access denied to this workspace", 403)
    if body.memory_scope and not body.memory_scope.startswith("workspace:"):
        return error_response("INVALID_SCOPE", "Session context needs a workspace scope", 400)
    readable = readable_workspace_scopes(ctx)
    if body.memory_scope and body.memory_scope not in readable:
        readable.append(body.memory_scope)  # ctx.can already allowed it above
    result = session_context_service.build_session_context(
        agent_id=ctx.agent_id,
        user_id=ctx.user_id or "",
        readable_scopes=readable,
        memory_scope=body.memory_scope,
        cwd=body.cwd,
        host_session_ref=body.host_session_ref,
        source=body.source,
        budget=body.max_chars,
        channel="rest",
    )
    return success_response(result)


class HandoffCheckRequest(BaseModel):
    host_session_ref: str = Field(min_length=1, max_length=200)


@router.post("/handoff-check")
def handoff_check(
    body: HandoffCheckRequest, ctx: EffectiveAuthority = Depends(get_request_context)
):
    """Called by a Stop hook: should this session record a handoff before stopping?"""
    if not ctx.agent_id:
        return error_response("AGENT_REQUIRED", "Handoff check requires an agent", 400)
    return success_response(
        session_context_service.handoff_check(
            agent_id=ctx.agent_id,
            user_id=ctx.user_id or "",
            host_session_ref=body.host_session_ref,
        )
    )
