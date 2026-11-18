from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from typing import Optional

from app.services import audit_service, workspace_service
from app.services import agent_access_service, agent_kind, agent_service
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
    # Subset of read_scopes the agent recalls from on an unscoped search. None =
    # default (fan all read_scopes).
    default_recall_scopes: Optional[list[str]] = None
    capabilities: Optional[list[str]] = None
    # Whether it may also run the services bound in its person's own scope.
    inherit_user_bindings: bool = False
    # "agent" or "service" (a program that only fetches the secrets it is granted). Left out: a service when the
    # permissions are exactly credentials_resolve (how such entries were always created), otherwise an agent.
    kind: Optional[str] = None


class UpdateAgentRequest(BaseModel):
    # A field this model does not know used to be dropped and still answered "Agent updated"
    # (for example is_active: deactivating is DELETE). Refuse it so the caller finds out.
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = None
    description: Optional[str] = None
    read_scopes: Optional[list[str]] = None
    write_scopes: Optional[list[str]] = None
    # None = leave unchanged. Provide a list to narrow; set reset flag to clear
    # back to the default (fan all read_scopes).
    default_recall_scopes: Optional[list[str]] = None
    reset_default_recall_scopes: bool = False
    capabilities: Optional[list[str]] = None
    inherit_user_bindings: Optional[bool] = None
    # Change an entry between "agent" and "service" (see the rules in app/services/agent_kind.py).
    kind: Optional[str] = None


def _may_run_persons_services(session: dict, person_id: Optional[str]) -> bool:
    """Letting an agent run a person's own services is that person's call, or an admin's."""
    return _is_admin(session) or session["user_id"] == person_id


def _is_admin(session: dict) -> bool:
    return session.get("role") == "admin"


def _can_manage_agent(agent: dict, session: dict) -> bool:
    return _is_admin(session) or agent.get("owner_user_id") == session["user_id"]


def _can_view_agent(agent: dict, session: dict) -> bool:
    if _can_manage_agent(agent, session):
        return True
    return agent_service.is_agent_shared(agent)


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
        if scope.startswith("user:"):
            if _is_admin(session) or scope == f"user:{owner_user_id}":
                continue
            return error_response("FORBIDDEN", "Only an administrator may grant another user's personal scope.", 403)
        if _is_admin(session):
            continue
        if scope == "shared":
            if write:
                return error_response("FORBIDDEN", "Only admins can grant shared write access", 403)
            continue
        if scope.startswith("workspace:"):
            workspace = workspace_service.get_workspace_by_id(scope.split(":", 1)[1])
            if write:
                if workspace and workspace_service.can_user_write_workspace(session["user_id"], workspace["id"]):
                    continue
            elif workspace and workspace_service.can_user_read_workspace(session["user_id"], workspace["id"]):
                continue
            if workspace and workspace.get("owner_user_id") == session["user_id"]:
                continue
        if scope.startswith("agent:"):
            agent = agent_service.get_agent_by_id(scope.split(":", 1)[1])
            if agent and agent.get("owner_user_id") == session["user_id"]:
                continue
        return error_response("FORBIDDEN", f"Cannot grant scope: {scope}", 403)

    return None


def _authority_audit_details(agent: dict) -> dict:
    """Safe, durable description of an agent's permanent authority."""
    return {
        "read_scopes": agent_service.parse_scopes(agent.get("read_scopes_json", "[]")),
        "write_scopes": agent_service.parse_scopes(agent.get("write_scopes_json", "[]")),
        "default_recall_scopes": (
            agent_service.parse_scopes(agent["default_recall_scopes_json"])
            if agent.get("default_recall_scopes_json")
            else None
        ),
        "capabilities": agent_service.parse_capabilities(agent.get("capabilities_json")),
        "inherit_user_bindings": bool(agent.get("inherit_user_bindings")),
        "kind": agent.get("kind") or "agent",
    }


@router.get("")
def list_agents(kind: Optional[str] = None, session: dict = Depends(get_current_session)):
    """Every entry by default; ``?kind=agent`` or ``?kind=service`` lists one kind."""
    if kind is not None and kind not in agent_kind.KINDS:
        return error_response("INVALID_KIND", "kind must be 'agent' or 'service'", 400)
    if _is_admin(session):
        agents = agent_service.list_agents(kind=kind)
    else:
        agents = agent_service.list_visible_agents(session["user_id"], kind=kind)
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
            "default_recall_scopes_json": agent.get("default_recall_scopes_json"),
            "capabilities": agent_service.parse_capabilities(agent.get("capabilities_json")),
            "is_active": agent["is_active"],
            "is_shared": agent_service.is_agent_shared(agent),
            "created_at": agent["created_at"],
            "kind": agent.get("kind") or "agent",
        })
    return success_response({"agents": safe_agents})


@router.post("")
def create_agent(
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
    # A service fetches secrets and belongs to no workspace, so the one-workspace default below does not apply to it.
    makes_service = body.kind == "service" or (
        body.kind is None and sorted(set(body.capabilities or [])) == agent_kind.SERVICE_PERMISSIONS
    )
    if body.read_scopes is None and body.write_scopes is None and not makes_service:
        # An install with exactly one workspace: an agent created with defaults works in it, so two such agents
        # share something without anyone ticking boxes. With more than one, the person chooses.
        from app.services import workspace_service

        only = [w for w in workspace_service.list_workspaces(user_id=owner_user_id) if w.get("is_active")]
        if len(only) == 1:
            ws_scope = f"workspace:{only[0]['id']}"
            body.read_scopes = [f"agent:{normalized_id}", f"user:{owner_user_id}", "shared", ws_scope]
            body.write_scopes = [f"agent:{normalized_id}", ws_scope]
    scope_error = _validate_agent_scopes(body.read_scopes, session, agent_id=normalized_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.write_scopes, session, agent_id=normalized_id, owner_user_id=owner_user_id, write=True)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.default_recall_scopes, session, agent_id=normalized_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error

    try:
        agent, _ = agent_service.create_agent(
            agent_id=body.id,
            display_name=body.display_name,
            owner_user_id=session["user_id"],
            description=body.description or "",
            default_user_id=body.default_user_id,
            read_scopes=body.read_scopes,
            write_scopes=body.write_scopes,
            default_recall_scopes=body.default_recall_scopes,
            capabilities=body.capabilities,
            inherit_user_bindings=body.inherit_user_bindings,
            issue_key=False,
            kind=body.kind,
        )
    except agent_kind.AgentKindError as exc:
        return error_response("SERVICE_RULE", str(exc), 400)
    except ValueError as exc:
        return error_response("INVALID_CAPABILITIES", str(exc), 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_created",
        resource_type="agent",
        resource_id=agent["id"],
        result="success",
        details=_authority_audit_details(agent),
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
            "default_recall_scopes_json": agent.get("default_recall_scopes_json"),
            "capabilities": agent_service.parse_capabilities(agent.get("capabilities_json")),
            "is_active": agent["is_active"],
            "kind": agent.get("kind") or "agent",
        },
        "next_step": "Generate a one-time connection key and config from the Connect page.",
    }, status_code=201)


@router.get("/{agent_id}")
def get_agent(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_view_agent(agent, session):
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
            "default_recall_scopes_json": agent.get("default_recall_scopes_json"),
            "capabilities": agent_service.parse_capabilities(agent.get("capabilities_json")),
            "is_active": agent["is_active"],
            "created_at": agent["created_at"],
            "is_shared": agent_service.is_agent_shared(agent),
            "inherit_user_bindings": bool(agent.get("inherit_user_bindings")),
            "kind": agent.get("kind") or "agent",
            # Workspaces in its lists that its person cannot reach, so they have no effect yet.
            "no_effect": agent_access_service.no_effect(agent),
        }
    })


@router.put("/{agent_id}")
def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    session: dict = Depends(get_current_session),
):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)
    if body.inherit_user_bindings and not _may_run_persons_services(session, agent.get("default_user_id") or agent.get("owner_user_id")):
        return error_response(
            "FORBIDDEN", "Only an admin, or the person the agent acts for, can let it run that person's own services", 403
        )

    owner_user_id = agent.get("owner_user_id") or session["user_id"]
    scope_error = _validate_agent_scopes(body.read_scopes, session, agent_id=agent_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.write_scopes, session, agent_id=agent_id, owner_user_id=owner_user_id, write=True)
    if scope_error:
        return scope_error
    scope_error = _validate_agent_scopes(body.default_recall_scopes, session, agent_id=agent_id, owner_user_id=owner_user_id)
    if scope_error:
        return scope_error

    update_kwargs = dict(
        display_name=body.display_name,
        description=body.description,
        read_scopes=body.read_scopes,
        write_scopes=body.write_scopes,
        capabilities=body.capabilities,
        inherit_user_bindings=body.inherit_user_bindings,
        kind=body.kind,
    )
    # reset flag clears back to default (fan all read_scopes); otherwise a
    # provided list narrows, and absence leaves it unchanged.
    if body.reset_default_recall_scopes:
        update_kwargs["default_recall_scopes"] = None
    elif body.default_recall_scopes is not None:
        update_kwargs["default_recall_scopes"] = body.default_recall_scopes
    try:
        agent_service.update_agent(agent_id, **update_kwargs)
    except agent_kind.AgentKindError as exc:
        return error_response("SERVICE_RULE", str(exc), 400)
    except ValueError as exc:
        return error_response("INVALID_CAPABILITIES", str(exc), 400)
    updated_agent = agent_service.get_agent_by_id(agent_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_updated",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        details=_authority_audit_details(updated_agent or agent),
    )

    return success_response({"message": "Agent updated"})


@router.post("/{agent_id}/permissions/{permission}/turn-on")
def turn_on_permission(agent_id: str, permission: str, session: dict = Depends(get_current_session)):
    """Switch one permission on, by a person, as one recorded action. Nothing switches a permission on by itself, and
    nothing here ever switches one off."""
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)
    before = agent_service.parse_capabilities(agent.get("capabilities_json"))
    try:
        after = agent_service.turn_on_permission(agent_id, permission)
    except agent_kind.AgentKindError as exc:
        return error_response("SERVICE_RULE", str(exc), 400)
    except ValueError as exc:
        return error_response("INVALID_PERMISSION", str(exc), 400)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_permission_turned_on",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        details={"permission": permission, "permissions_before": before, "permissions_after": after},
    )
    return success_response({"permissions": after})


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, session: dict = Depends(get_current_session)):
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
def activate_agent(agent_id: str, session: dict = Depends(get_current_session)):
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
def purge_agent(agent_id: str, session: dict = Depends(get_current_session)):
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
def rotate_key(agent_id: str, session: dict = Depends(get_current_session)):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not _can_manage_agent(agent, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    new_key = agent_service.rotate_agent_key(agent_id, created_by=session["user_id"])
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


def _public_key(record: dict) -> dict:
    """A key as the dashboard and API show it: never the key itself or its hash."""
    return {k: record.get(k) for k in (
        "id", "label", "key_hint", "created_at", "created_by", "last_used_at", "revoked_at", "revoked_by")}


class AddKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class RenameKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


def _manageable_agent(agent_id: str, session: dict):
    agent = agent_service.get_agent_by_id(agent_id)
    if not agent:
        return None, error_response("NOT_FOUND", "Agent not found", 404)
    if not _can_manage_agent(agent, session):
        return None, error_response("FORBIDDEN", "Access denied", 403)
    return agent, None


@router.get("/{agent_id}/keys")
def list_keys(agent_id: str, include_revoked: bool = False, session: dict = Depends(get_current_session)):
    _, error = _manageable_agent(agent_id, session)
    if error:
        return error
    keys = agent_service.list_agent_keys(agent_id, include_revoked=include_revoked)
    return success_response({"keys": [_public_key(k) for k in keys]})


@router.post("/{agent_id}/keys", status_code=201)
def add_key(agent_id: str, body: AddKeyRequest, session: dict = Depends(get_current_session)):
    """Add one more key to an agent; its other keys keep working. The key is returned once."""
    _, error = _manageable_agent(agent_id, session)
    if error:
        return error
    label = body.label.strip()
    if not label:
        return error_response("INVALID_LABEL", "Give the key a label, such as the machine or tool that will use it", 400)
    try:
        record, plaintext = agent_service.add_agent_key(agent_id, label, created_by=session["user_id"])
    except agent_service.AgentKeyError as exc:
        return error_response(exc.code, str(exc), 404 if exc.code == "NOT_FOUND" else 400)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_created",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        details={"record_id": record["id"], "label": record["label"]},
    )
    return success_response({"key": _public_key(record), "api_key": plaintext}, status_code=201)


@router.patch("/{agent_id}/keys/{key_id}")
def rename_key(agent_id: str, key_id: str, body: RenameKeyRequest, session: dict = Depends(get_current_session)):
    """Change a key's label. Only the label changes; the key keeps working as before."""
    _, error = _manageable_agent(agent_id, session)
    if error:
        return error
    label = body.label.strip()
    if not label:
        return error_response("INVALID_LABEL", "Give the key a label, such as the machine or tool that uses it", 400)
    existing = agent_service.get_agent_key(agent_id, key_id)
    record = agent_service.rename_agent_key(agent_id, key_id, label)
    if not record:
        return error_response("NOT_FOUND", "No such key, or it is revoked", 404)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_renamed",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        details={"record_id": key_id, "label": record["label"], "previous_label": (existing or {}).get("label")},
    )
    return success_response({"key": _public_key(record)})


@router.delete("/{agent_id}/keys/{key_id}")
def revoke_key(agent_id: str, key_id: str, session: dict = Depends(get_current_session)):
    """Revoke one key. The agent's other keys keep working."""
    _, error = _manageable_agent(agent_id, session)
    if error:
        return error
    existing = agent_service.get_agent_key(agent_id, key_id)
    if not agent_service.revoke_agent_key(agent_id, key_id, revoked_by=session["user_id"]):
        return error_response("NOT_FOUND", "No such key, or it is already revoked", 404)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_revoked",
        resource_type="agent",
        resource_id=agent_id,
        result="success",
        details={"record_id": key_id, "label": (existing or {}).get("label")},
    )
    return success_response({"message": "Key revoked"})
