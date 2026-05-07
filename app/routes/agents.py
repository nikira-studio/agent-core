from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import audit_service, workspace_service
from app.services import agent_service
from app.security.dependencies import get_current_session
from app.security.response_helpers import success_response, error_response
from app.security.scope_utils import validate_scope_string, normalize_scope_string
from app.models.enums import normalize_id


router = APIRouter(prefix="/api/agents", tags=["agents"])


class CreateAgentRequest(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = ""
    default_user_id: Optional[str] = None
    read_scopes: Optional[list[str]] = None
    write_scopes: Optional[list[str]] = None


class UpdateAgentRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    read_scopes: Optional[list[str]] = None
    write_scopes: Optional[list[str]] = None


def get_agent_auth(authorization: str = "") -> tuple[str, str]:
    if not authorization.startswith("Bearer "):
        return None, None
    api_key = authorization[7:]
    if not api_key.startswith("ac_sk_"):
        return None, None
    return api_key, None


def _is_admin(session: dict) -> bool:
    return session.get("role") == "admin"


def _can_manage_agent(agent: dict, session: dict) -> bool:
    return _is_admin(session) or agent.get("owner_user_id") == session["user_id"]


def _validate_agent_scopes(
    scopes: list[str] | None,
    session: dict,
    *,
    agent_id: str,
    owner_user_id: str,
    write: bool = False,
):
    if scopes is None:
        return None

    normalized_agent_id = normalize_id(agent_id)
    own_scope = f"agent:{normalized_agent_id}"
    for raw_scope in scopes:
        if not validate_scope_string(raw_scope):
            return error_response("INVALID_SCOPE", f"Invalid scope: {raw_scope}", 400)

        scope = normalize_scope_string(raw_scope)
        if scope == own_scope:
            continue
        if scope == f"user:{owner_user_id}":
            continue
        if scope.startswith("user:"):
            return error_response("FORBIDDEN", "Agents can only be granted their owner's personal user scope. Use workspace scopes for collaboration.", 403)
        if _is_admin(session):
            continue
        if scope == "shared":
            if write:
                return error_response("FORBIDDEN", "Only admins can grant shared write access", 403)
            continue
        if scope.startswith("workspace:"):
            workspace = workspace_service.get_workspace_by_id(scope.split(":", 1)[1])
            if workspace and workspace.get("owner_user_id") == session["user_id"]:
                continue
        if scope.startswith("agent:"):
            agent = agent_service.get_agent_by_id(scope.split(":", 1)[1])
            if agent and agent.get("owner_user_id") == session["user_id"]:
                continue
        return error_response("FORBIDDEN", f"Cannot grant scope: {scope}", 403)

    return None


@router.get("")
async def list_agents(session: dict = Depends(get_current_session)):
    owner_user_id = None if _is_admin(session) else session["user_id"]
    agents = agent_service.list_agents(owner_user_id=owner_user_id)
    safe_agents = []
    for agent in agents:
        safe_agents.append({
            "id": agent["id"],
            "display_name": agent["display_name"],
            "description": agent["description"],
            "owner_user_id": agent["owner_user_id"],
            "default_user_id": agent.get("default_user_id"),
            "read_scopes_json": agent["read_scopes_json"],
            "write_scopes_json": agent["write_scopes_json"],
            "is_active": agent["is_active"],
            "created_at": agent["created_at"],
        })
    return success_response({"agents": safe_agents})


@router.post("")
async def create_agent(
    body: CreateAgentRequest,
    session: dict = Depends(get_current_session),
):
    try:
        normalized_id = normalize_id(body.id)
    except ValueError as e:
        return error_response("INVALID_ID", str(e), 400)

    existing = agent_service.get_agent_by_id(normalized_id)
    if existing:
        return error_response("AGENT_EXISTS", "Agent ID already exists", 400)

    if not _is_admin(session) and body.default_user_id not in (None, session["user_id"]):
        return error_response("FORBIDDEN", "Cannot assign another user's context", 403)

    owner_user_id = session["user_id"]
    scope_error = _validate_agent_scopes(body.read_scopes, session, agent_id=normalized_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.write_scopes, session, agent_id=normalized_id, owner_user_id=owner_user_id, write=True)
    if scope_error:
        return scope_error

    agent, _api_key_plaintext = agent_service.create_agent(
        agent_id=body.id,
        display_name=body.display_name,
        owner_user_id=session["user_id"],
        description=body.description or "",
        default_user_id=body.default_user_id,
        read_scopes=body.read_scopes,
        write_scopes=body.write_scopes,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_created",
        resource_type="agent",
        resource_id=agent["id"],
        result="success",
    )

    return success_response({
        "agent": {
            "id": agent["id"],
            "display_name": agent["display_name"],
            "description": agent["description"],
            "owner_user_id": agent["owner_user_id"],
            "default_user_id": agent.get("default_user_id"),
            "read_scopes_json": agent["read_scopes_json"],
            "write_scopes_json": agent["write_scopes_json"],
            "is_active": agent["is_active"],
        },
        "next_step": "Generate a one-time connection key and config from Integrations.",
    }, status_code=201)


@router.get("/{agent_id}")
async def get_agent(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if agent["owner_user_id"] != session["user_id"] and session.get("role") != "admin":
        return error_response("FORBIDDEN", "Access denied", 403)

    return success_response({
        "agent": {
            "id": agent["id"],
            "display_name": agent["display_name"],
            "description": agent["description"],
            "owner_user_id": agent["owner_user_id"],
            "default_user_id": agent.get("default_user_id"),
            "read_scopes_json": agent["read_scopes_json"],
            "write_scopes_json": agent["write_scopes_json"],
            "is_active": agent["is_active"],
            "created_at": agent["created_at"],
        }
    })


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    session: dict = Depends(get_current_session),
):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    owner_user_id = agent.get("owner_user_id") or session["user_id"]
    scope_error = _validate_agent_scopes(body.read_scopes, session, agent_id=agent_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.write_scopes, session, agent_id=agent_id, owner_user_id=owner_user_id, write=True)
    if scope_error:
        return scope_error

    agent_service.update_agent(
        agent_id,
        display_name=body.display_name,
        description=body.description,
        read_scopes=body.read_scopes,
        write_scopes=body.write_scopes,
    )

    return success_response({"message": "Agent updated"})


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    agent_service.deactivate_agent(agent_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_deactivated",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )

    return success_response({"message": "Agent deactivated"})


@router.post("/{agent_id}/activate")
async def activate_agent(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    agent_service.reactivate_agent(agent_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_reactivated",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )

    return success_response({"message": "Agent reactivated"})


@router.post("/{agent_id}/purge")
async def purge_agent(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    agent_service.delete_agent_hard(agent_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_purged",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )

    return success_response({"message": "Agent permanently deleted"})


@router.post("/{agent_id}/rotate_key")
async def rotate_key(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    new_key = agent_service.rotate_agent_key(agent_id)
    if not new_key:
        return error_response("AGENT_INACTIVE", "Cannot rotate key for inactive agent", 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_rotated",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
    )

    return success_response({"api_key": new_key})
