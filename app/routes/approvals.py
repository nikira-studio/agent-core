"""Approvals API: agents propose admin changes; a signed-in person decides."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.exceptions import APIError
from app.security.response_helpers import error_response, success_response
from app.security.scope_enforcer import ScopeEnforcer
from app.services import approval_service
from app.services.auth_service import validate_session

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ProposedOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    title: str
    note: Optional[str] = None
    operations: list[ProposedOperation]
    execution_id: Optional[str] = None
    expiry_hours: int = approval_service.DEFAULT_EXPIRY_HOURS


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: Optional[str] = None
    # Values the person enters for operations' person fields, keyed
    # "<operation index>.<field>", e.g. {"0.value": "..."} for a credential.
    values: dict[str, str] = Field(default_factory=dict)


def _enforcer(ctx: EffectiveAuthority) -> ScopeEnforcer:
    return ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id, is_admin=ctx.is_admin,
                         active_workspace_ids=ctx.active_workspace_ids)


def propose_for(ctx: EffectiveAuthority, body: ProposalRequest) -> dict:
    """Shared by REST and MCP."""
    if ctx.actor_type != "agent":
        raise APIError("AGENT_ONLY", "Proposals come from agents; people make admin changes directly", 403)
    if not ctx.has_capability("admin_propose"):
        raise APIError("CAPABILITY_DENIED", "This operation requires the admin_propose capability", 403)
    if not _enforcer(ctx).can_write(body.scope):
        raise APIError("SCOPE_DENIED", f"Access denied to scope: {body.scope}", 403)
    return approval_service.propose(
        agent_id=ctx.agent_id, scope=body.scope, title=body.title, note=body.note,
        operations=[o.model_dump() for o in body.operations],
        execution_id=body.execution_id, expiry_hours=body.expiry_hours,
    )


def view_for(ctx: EffectiveAuthority, approval_id: str) -> dict:
    row = approval_service.get(approval_id)
    if row is None or not (row["proposed_by"] == ctx.agent_id or _enforcer(ctx).can_read(row["scope"])):
        raise APIError("NOT_FOUND", "No such approval", 404)
    view = approval_service.public_view(row)
    from app.services import connector_approval_service

    message = connector_approval_service.terminal_message(row)
    if message:
        view["message"] = message
    return view


@router.post("")
def propose(body: ProposalRequest, ctx: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"approval": propose_for(ctx, body)}, status_code=201)


def _browser_session(request: Request) -> dict:
    """The dashboard cookie session, for managing trusted devices. A trusted
    device cannot trust more devices."""
    if request.headers.get("Authorization"):
        raise APIError("COOKIE_SESSION_REQUIRED", "Use the dashboard", 403)
    session = validate_session(request.cookies.get("session_token") or "",
                               inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES)
    if not session:
        raise APIError("UNAUTHORIZED", "Sign in to the dashboard to manage trusted devices", 401)
    return session


@router.post("/trust-device")
def trust_this_device(request: Request):
    """Make the browser a trusted device: it can open and decide approvals
    without signing in, for settings.TRUSTED_DEVICE_DAYS, and for nothing else."""
    from fastapi.responses import JSONResponse

    from app.routes.auth import get_client_ip, _request_is_https
    from app.services import audit_service
    from app.services.auth_service import TRUSTED_DEVICE_COOKIE, TRUSTED_DEVICE_COOKIE_PATHS, trust_device

    if not settings.TRUSTED_DEVICES_ENABLED:
        raise APIError("TRUSTED_DEVICES_OFF", "Trusted devices are turned off on this install", 403)
    session = _browser_session(request)
    device = trust_device(session["user_id"])
    response = JSONResponse({"ok": True, "data": {"device_id": device["db_session_id"],
                                                  "expires_at": device["expires_at"]}})
    for path in TRUSTED_DEVICE_COOKIE_PATHS:
        response.set_cookie(
            key=TRUSTED_DEVICE_COOKIE, value=device["session_id"], path=path,
            max_age=settings.TRUSTED_DEVICE_DAYS * 86400, httponly=True, samesite="strict",
            secure=settings.COOKIE_SECURE or _request_is_https(request),
        )
    audit_service.write_event(actor_type="user", actor_id=session["user_id"], action="trusted_device_added",
                              resource_type="session", resource_id=device["db_session_id"], result="success",
                              ip_address=get_client_ip(request))
    return response


@router.get("/trusted-devices")
def trusted_devices(request: Request):
    from app.services.auth_service import list_trusted_devices

    return success_response({"devices": list_trusted_devices(_browser_session(request)["user_id"])})


@router.delete("/trusted-devices/{device_id}")
def revoke_trusted_device_route(device_id: str, request: Request):
    from app.routes.auth import get_client_ip
    from app.services import audit_service
    from app.services.auth_service import revoke_trusted_device

    session = _browser_session(request)
    if not revoke_trusted_device(session["user_id"], device_id):
        return error_response("NOT_FOUND", "No such trusted device", 404)
    audit_service.write_event(actor_type="user", actor_id=session["user_id"], action="trusted_device_revoked",
                              resource_type="session", resource_id=device_id, result="success",
                              ip_address=get_client_ip(request))
    return success_response({"revoked": device_id})


@router.get("")
def list_approvals(
    scope: Optional[list[str]] = Query(None),
    status: Optional[str] = "pending",
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    enforcer = _enforcer(ctx)
    scopes = scope or list(ctx.read_scopes)
    denied = next((s for s in scopes if not enforcer.can_read(s)), None)
    if denied:
        return error_response("SCOPE_DENIED", f"Access denied to scope: {denied}", 403)
    return success_response({"approvals": approval_service.list_for_scopes(scopes, status or None)})


@router.get("/{approval_id}")
def get_approval(approval_id: str, ctx: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"approval": view_for(ctx, approval_id)})


def _decider(request: Request) -> dict:
    """The person deciding: browser cookie only.

    Any Authorization header is refused, so no agent key, operator token or
    Bearer login session can decide, even one a script holds for the same
    person. The CSRF middleware has already checked the token, and the origin
    must be this server.
    """
    if request.headers.get("Authorization"):
        raise APIError("COOKIE_SESSION_REQUIRED", "Approvals are decided in the dashboard", 403)
    origin = request.headers.get("Origin")
    if origin:
        from app.security.public_url import public_base_url

        if origin.rstrip("/") != public_base_url(request):
            raise APIError("ORIGIN_MISMATCH", "Approvals are decided from this server's own pages", 403)
    from app.services.auth_service import approvals_session

    session = approvals_session(request)
    if not session:
        raise APIError("UNAUTHORIZED", "Authentication required", 401)
    return session


@router.post("/{approval_id}/decide")
def decide(approval_id: str, body: DecisionRequest, request: Request):
    from app.security.rate_limiter import RL

    session = _decider(request)
    allowed, _ = RL.check("user", session["user_id"], "approval_decide")
    if not allowed:
        return error_response("RATE_LIMITED", "Too many decisions; wait a moment", 429)
    result = approval_service.decide(approval_id, session=session, approve=body.approve, note=body.note,
                                     person_values=body.values, request=request)
    return success_response({"approval": result})
