"""Who can reach what: the read side behind the Credential, Agent, Capability and Service pages, and the previews.

Admin session only. Names, ids and yes/no answers; no secret value. Every answer comes from
``app/services/reach_service.py``, the same code the request path and the upgrade check use.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.security.dependencies import require_admin
from app.security.response_helpers import error_response, success_response
from app.security.scope_utils import validate_scope_string, normalize_scope_string
from app.database import get_db
from app.services import (
    access_overview_service, agent_service, audience_service, audit_service, connector_service, credential_service,
    reach_service,
)

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
    owned = credential_service.list_credentials(scope=f"capability:{binding['id']}", limit=100)
    owned_info = None
    if owned:
        from app.database import get_db as _get_db
        from app.services.credential_consumer_service import is_expired

        with _get_db() as conn:
            holders = [
                r["display_name"] or r["consumer_agent_id"]
                for r in conn.execute(
                    "SELECT g.consumer_agent_id, g.expires_at, a.display_name FROM credential_consumer_grants g "
                    "LEFT JOIN agents a ON a.id = g.consumer_agent_id WHERE g.entry_id = ? AND g.revoked_at IS NULL",
                    (owned[0]["id"],),
                ).fetchall()
                if not is_expired({"expires_at": r["expires_at"]})
            ]
        owned_info = {"id": owned[0]["id"], "name": owned[0]["name"], "services_holding": holders}
    return success_response({
        "owned_credential": owned_info,
        "connection": {"id": binding["id"], "name": binding["name"], "capability": binding.get("connector_display_name")},
        "credential": {"id": credential["id"], "name": credential["name"]} if credential else None,
        "users": reach_service.agents_using_connection(binding),
        "audience": reach_service.connection_audience(binding),
        "audience_options": reach_service.audience_options(),
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


# ── who may use a connection ────────────────────────────────────────────────


class AudienceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=32)
    ref: str = Field(min_length=1, max_length=128)
    exclude: bool = False


class AudienceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: list[AudienceEntry]


def _clean(entries: list[AudienceEntry]):
    with get_db() as conn:
        return audience_service.validate(conn, [e.model_dump() for e in entries])


def _write_audience(binding: dict, clean: list[dict], session: dict, how: str):
    before = audience_service.entries_for(binding["id"])
    with get_db() as conn:
        audience_service.replace(conn, binding["id"], clean)
        conn.commit()
    after = audience_service.entries_for(binding["id"])
    if before != after:
        audit_service.write_event(
            actor_type="user", actor_id=session["user_id"], action="connector_audience_changed",
            resource_type="connector_binding", resource_id=binding["id"], result="success",
            details={"before": before, "after": after, "via": how},
        )
    return after


class AudiencePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str = Field(min_length=1, max_length=128)
    audience: list[AudienceEntry]


@router.post("/preview/audience")
def preview_audience(body: AudiencePreview, session: dict = Depends(require_admin)):
    binding = connector_service.get_binding(body.binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Connection not found", 404)
    clean, problems = _clean(body.audience)
    if problems:
        return error_response("INVALID_AUDIENCE", " ".join(problems), 400)
    return success_response(reach_service.preview_audience(binding, clean))


@router.put("/connections/{binding_id}/audience")
def replace_audience(binding_id: str, body: AudienceBody, session: dict = Depends(require_admin)):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Connection not found", 404)
    clean, problems = _clean(body.audience)
    if problems:
        return error_response("INVALID_AUDIENCE", " ".join(problems), 400)
    _write_audience(binding, clean, session, "replace")
    return success_response({"audience": reach_service.connection_audience(binding), "users": reach_service.agents_using_connection(binding)})


@router.post("/connections/{binding_id}/audience")
def add_audience_entry(binding_id: str, body: AudienceEntry, session: dict = Depends(require_admin)):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Connection not found", 404)
    current = [e for e in audience_service.entries_for(binding_id) if not (e["kind"] == body.kind and e["ref"] == body.ref)]
    clean, problems = _clean([AudienceEntry(**e) for e in current] + [body])
    if problems:
        return error_response("INVALID_AUDIENCE", " ".join(problems), 400)
    _write_audience(binding, clean, session, "add")
    return success_response({"audience": reach_service.connection_audience(binding), "users": reach_service.agents_using_connection(binding)})


@router.delete("/connections/{binding_id}/audience/{kind}/{ref}")
def remove_audience_entry(binding_id: str, kind: str, ref: str, session: dict = Depends(require_admin)):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Connection not found", 404)
    current = audience_service.entries_for(binding_id)
    kept = [e for e in current if not (e["kind"] == kind and e["ref"] == ref)]
    if len(kept) == len(current):
        return error_response("NOT_FOUND", "That entry is not in the audience", 404)
    _write_audience(binding, kept, session, "remove")
    return success_response({"audience": reach_service.connection_audience(binding), "users": reach_service.agents_using_connection(binding)})
