"""Who can reach what: the read side behind the Credential, Agent, Capability and Service pages, and the previews.

Admin session only. Names, ids and yes/no answers; no secret value. Every answer comes from
``app/services/reach_service.py``, the same code the request path and the upgrade check use.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.security.dependencies import require_admin
from app.security.response_helpers import error_response, success_response
from app.security.scope_utils import validate_scope_string, normalize_scope_string
from app.services import access_overview_service, agent_service, connector_service, credential_service, reach_service

router = APIRouter(prefix="/api/access", tags=["access"])


@router.get("/overview")
def overview(session: dict = Depends(require_admin)):
    """Every assignment in one list with when it was last used, and what nothing uses."""
    return success_response(access_overview_service.overview())


@router.get("/credentials/{entry_id}")
def credential_access(entry_id: str, session: dict = Depends(require_admin)):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential not found", 404)
    return success_response(reach_service.credential_access(entry))


@router.get("/connections/{binding_id}")
def connection_access(binding_id: str, session: dict = Depends(require_admin)):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Connection not found", 404)
    credential = credential_service.get_credential(binding["credential_id"]) if binding.get("credential_id") else None
    return success_response({
        "connection": {"id": binding["id"], "name": binding["name"], "capability": binding.get("connector_display_name")},
        "credential": {"id": credential["id"], "name": credential["name"]} if credential else None,
        "users": reach_service.agents_using_connection(binding),
    })


@router.get("/agents/{agent_id}")
def agent_access(agent_id: str, session: dict = Depends(require_admin)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent or (agent.get("kind") or "agent") != "agent":
        return error_response("NOT_FOUND", "Agent not found", 404)
    return success_response(reach_service.agent_summary(agent))


@router.get("/services/{service_id}")
def service_access(service_id: str, session: dict = Depends(require_admin)):
    service = agent_service.get_agent_by_id(service_id)
    if not service or (service.get("kind") or "agent") != "service":
        return error_response("NOT_FOUND", "Service not found", 404)
    return success_response(reach_service.service_summary(service))


class OwnerPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=200)


@router.post("/preview/credential-owner")
def preview_credential_owner(body: OwnerPreview, session: dict = Depends(require_admin)):
    entry = credential_service.get_credential(body.entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential not found", 404)
    if not validate_scope_string(body.scope):
        return error_response("INVALID_SCOPE", "Malformed owner", 400)
    return success_response(reach_service.preview_credential_owner(entry, normalize_scope_string(body.scope)))


class WorkspacePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    action: str = Field(default="add", pattern="^(add|remove)$")
    read: bool = True
    write: bool = False


@router.post("/preview/agent-workspace")
def preview_agent_workspace(body: WorkspacePreview, session: dict = Depends(require_admin)):
    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent or (agent.get("kind") or "agent") != "agent":
        return error_response("NOT_FOUND", "Agent not found", 404)
    return success_response(
        reach_service.preview_agent_workspace(agent, body.workspace_id, body.action, read=body.read, write=body.write)
    )
