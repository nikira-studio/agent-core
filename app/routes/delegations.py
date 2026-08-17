from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.exceptions import APIError
from app.security.response_helpers import success_response
from app.services import audit_service, delegation_service

router = APIRouter(prefix="/api/delegations", tags=["delegations"])


class ScopePermission(BaseModel):
    resource_type: str
    operation: str
    scope: str


class ResourcePermission(BaseModel):
    resource_type: str
    operation: str
    resource_id: str


class BindingAction(BaseModel):
    binding_id: str
    action: str


class CreateGrantRequest(BaseModel):
    recipient_agent_id: str
    purpose: str
    ttl_seconds: int = Field(gt=0, le=3600)
    scope_permissions: list[ScopePermission] = []
    resource_permissions: list[ResourcePermission] = []
    binding_actions: list[BindingAction] = []
    coordinator_agent_id: str | None = None
    activity_id: str | None = None
    correlation_id: str | None = None


class RevokeGrantRequest(BaseModel):
    reason: str | None = None


@router.get("")
def list_grants(authority: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"grants": delegation_service.list_grants(authority)})


@router.get("/{grant_id}")
def get_grant(grant_id: str, authority: EffectiveAuthority = Depends(get_request_context)):
    return success_response({"grant": delegation_service.get_grant(grant_id, authority)})


@router.post("")
def create_grant(body: CreateGrantRequest, authority: EffectiveAuthority = Depends(get_request_context)):
    grant = delegation_service.create_grant(
        authority, recipient_agent_id=body.recipient_agent_id, purpose=body.purpose,
        ttl_seconds=body.ttl_seconds,
        scope_permissions=[item.model_dump() for item in body.scope_permissions],
        resource_permissions=[item.model_dump() for item in body.resource_permissions],
        binding_actions=[item.model_dump() for item in body.binding_actions],
        coordinator_agent_id=body.coordinator_agent_id, activity_id=body.activity_id,
        correlation_id=body.correlation_id,
    )
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_grant_created", "delegated_grant", grant["id"], details={"recipient_agent_id": grant["recipient_agent_id"]})
    return success_response({"grant": grant}, status_code=201)


@router.post("/{grant_id}/claim")
def claim_grant(grant_id: str, authority: EffectiveAuthority = Depends(get_request_context)):
    if authority.actor_type != "agent" or authority.is_delegated:
        raise APIError("FORBIDDEN", "Only an authenticated recipient may claim a grant", 403)
    grant, secret = delegation_service.claim_grant(grant_id, authority.agent_id)
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_grant_claimed", "delegated_grant", grant_id)
    return success_response({"grant": grant, "grant_secret": secret})


@router.post("/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: RevokeGrantRequest, authority: EffectiveAuthority = Depends(get_request_context)):
    grant = delegation_service.revoke_grant(grant_id, authority, body.reason)
    audit_service.write_event(authority.actor_type, authority.actor_id, "delegation_grant_revoked", "delegated_grant", grant_id)
    return success_response({"grant": grant})
