from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.routes.delegations import BindingAction, ResourcePermission, ScopePermission
from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.response_helpers import success_response
from app.services import audit_service, delegation_service

router = APIRouter(prefix="/api/delegation-requests", tags=["delegations"])


class CreateRequest(BaseModel):
    recipient_agent_id: str
    purpose: str
    ttl_seconds: int = Field(gt=0, le=3600)
    scope_permissions: list[ScopePermission] = []
    resource_permissions: list[ResourcePermission] = []
    binding_actions: list[BindingAction] = []
    activity_id: str | None = None
    correlation_id: str | None = None


class Approval(BaseModel):
    scope_permissions: list[ScopePermission] | None = None
    resource_permissions: list[ResourcePermission] | None = None
    binding_actions: list[BindingAction] | None = None


class Denial(BaseModel):
    reason: str | None = None


def _dump(items):
    return None if items is None else [item.model_dump() for item in items]


@router.post("")
async def create_request(body: CreateRequest, authority: EffectiveAuthority = Depends(get_request_context)):
    request = delegation_service.create_request(
        authority, recipient_agent_id=body.recipient_agent_id, purpose=body.purpose,
        ttl_seconds=body.ttl_seconds, scope_permissions=_dump(body.scope_permissions),
        resource_permissions=_dump(body.resource_permissions), binding_actions=_dump(body.binding_actions),
        activity_id=body.activity_id, correlation_id=body.correlation_id,
    )
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_request_created", "delegation_request", request["id"])
    return success_response({"request": request}, status_code=201)


@router.get("")
async def list_requests(authority: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"requests": delegation_service.list_requests(authority)})


@router.get("/{request_id}")
async def get_request(request_id: str, authority: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"request": delegation_service.get_request(request_id, authority)})


@router.post("/{request_id}/approve")
async def approve_request(request_id: str, body: Approval, authority: EffectiveAuthority = Depends(get_request_context)):
    result = delegation_service.approve_request(
        request_id, authority, scope_permissions=_dump(body.scope_permissions),
        resource_permissions=_dump(body.resource_permissions), binding_actions=_dump(body.binding_actions),
    )
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_request_approved", "delegation_request", request_id, details={"grant_id": result["grant"]["id"]})
    return success_response(result)


@router.post("/{request_id}/deny")
async def deny_request(request_id: str, body: Denial, authority: EffectiveAuthority = Depends(get_request_context)):
    request = delegation_service.deny_request(request_id, authority, body.reason)
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_request_denied", "delegation_request", request_id)
    return success_response({"request": request})
