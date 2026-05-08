from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import connector_service, audit_service
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer
from app.security.response_helpers import success_response, error_response


router = APIRouter(prefix="/api/connector-bindings", tags=["connector_bindings"])


class CreateBindingRequest(BaseModel):
    connector_type_id: str
    name: str
    scope: str
    credential_id: Optional[str] = None
    config_json: Optional[str] = None
    enabled: bool = True


class UpdateBindingRequest(BaseModel):
    name: Optional[str] = None
    scope: Optional[str] = None
    credential_id: Optional[str] = None
    config_json: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("")
async def list_bindings(
    scope: Optional[str] = None,
    connector_type_id: Optional[str] = None,
    enabled: Optional[bool] = None,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if scope and not enforcer.can_read(scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
    bindings = connector_service.list_bindings(
        scope=scope,
        connector_type_id=connector_type_id,
        enabled=enabled,
    )
    allowed = [b for b in bindings if enforcer.can_read(b["scope"])]
    return success_response({"bindings": allowed, "total": len(allowed)})


@router.post("")
async def create_binding(
    body: CreateBindingRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(body.scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
    ct = connector_service.get_connector_type(body.connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)
    binding = connector_service.create_binding(
        connector_type_id=body.connector_type_id,
        name=body.name,
        scope=body.scope,
        credential_id=body.credential_id,
        config_json=body.config_json,
        enabled=body.enabled,
        created_by=ctx.user_id,
    )
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_created",
        resource_type="connector_binding",
        resource_id=binding["id"],
        result="success",
    )
    return success_response({"binding": binding}, status_code=201)


@router.get("/{binding_id}")
async def get_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    return success_response({"binding": binding})


@router.put("/{binding_id}")
async def update_binding(
    binding_id: str,
    body: UpdateBindingRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    if body.scope and not enforcer.can_write(body.scope):
        return error_response("SCOPE_DENIED", "Access denied to new scope", 403)
    ok = connector_service.update_binding(
        binding_id,
        name=body.name,
        scope=body.scope,
        credential_id=body.credential_id,
        config_json=body.config_json,
        enabled=body.enabled,
    )
    if not ok:
        return error_response("UPDATE_FAILED", "No valid fields to update", 400)
    return success_response({"binding": connector_service.get_binding(binding_id)})


@router.delete("/{binding_id}")
async def delete_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    connector_service.delete_binding(binding_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_deleted",
        resource_type="connector_binding",
        resource_id=binding_id,
        result="success",
    )
    return success_response({"message": "Binding deleted"})


@router.post("/{binding_id}/test")
async def test_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    result = connector_service.test_binding(binding_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_tested",
        resource_type="connector_binding",
        resource_id=binding_id,
        result=result.get("success") and "success" or "failure",
    )
    return success_response({"result": result})


@router.get("/{binding_id}/executions")
async def list_binding_executions(
    binding_id: str,
    limit: int = 50,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    executions = connector_service.list_executions(
        binding_id, limit=limit, offset=offset
    )
    return success_response({"executions": executions, "total": len(executions)})
