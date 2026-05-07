from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.services import vault_service
from app.services import broker_service
from app.services import audit_service
from app.security.response_helpers import success_response, error_response
from app.time_utils import parse_utc_datetime, utc_now


router = APIRouter(prefix="/internal", tags=["internal"])


class ResolveRequest(BaseModel):
    variable_name: str
    agent_id: str


async def _verify_broker(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Broker "):
        return False
    token = authorization[7:]
    return broker_service.verify_broker_credential(token)


@router.post("/vault/resolve")
async def resolve_variable(
    body: ResolveRequest,
    authorization: str | None = Header(None),
):
    if not await _verify_broker(authorization):
        return error_response("UNAUTHORIZED", "Broker authentication required", 401)

    from app.services.agent_service import get_agent_by_id
    agent = get_agent_by_id(body.agent_id)
    if not agent or not agent.get("is_active"):
        return error_response("AGENT_NOT_FOUND", "Agent not found or inactive", 404)

    from app.security.scope_enforcer import ScopeEnforcer, build_agent_context
    ctx = build_agent_context(agent)

    entry = vault_service.get_vault_entry_by_reference(body.variable_name)
    if not entry:
        return error_response("NOT_FOUND", "Reference not found", 404)

    if entry.get("expires_at"):
        try:
            if utc_now() > parse_utc_datetime(entry["expires_at"]):
                return error_response("CREDENTIAL_EXPIRED", "Credential has expired", 410)
        except ValueError:
            pass

    enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id, is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)
    if not enforcer.can_read(entry["scope"]):
        return error_response("SCOPE_DENIED", "Agent does not have access to this scope", 403)

    plaintext = vault_service.resolve_reference(body.variable_name)
    if plaintext is None:
        return error_response("RESOLVE_FAILED", "Could not decrypt credential", 500)

    audit_service.write_event(
        actor_type="broker",
        actor_id=body.agent_id,
        action="broker_resolve",
        resource_type="vault_entry",
        resource_id=entry["id"],
        result="success",
    )

    return success_response({"value": plaintext})
