from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RequestContext:
    actor_type: str
    actor_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    read_scopes: list[str] = field(default_factory=list)
    write_scopes: list[str] = field(default_factory=list)
    active_workspace_ids: frozenset[str] = field(default_factory=frozenset)
    is_admin: bool = False


def build_user_context(session: dict) -> RequestContext:
    from app.services import workspace_service

    user_id = session["user_id"]
    is_admin = session.get("role") == "admin"
    workspaces = workspace_service.list_workspaces(
        owner_user_id=user_id, is_active=True
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes,
        active_workspace_ids=active_workspace_ids,
    )


def build_user_context_for_connectors(session: dict) -> RequestContext:
    from app.services import workspace_service

    user_id = session["user_id"]
    is_admin = session.get("role") == "admin"
    workspaces = workspace_service.list_workspaces(
        owner_user_id=user_id, is_active=True
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    binding_workspace_ids = workspace_service.get_workspace_ids_with_bindings()
    binding_workspace_scopes = [f"workspace:{wid}" for wid in binding_workspace_ids]

    all_active_workspace_ids = active_workspace_ids | binding_workspace_ids

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + workspace_scopes + binding_workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes + binding_workspace_scopes,
        active_workspace_ids=all_active_workspace_ids,
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    with get_db() as conn:
        binding_scopes = conn.execute(
            "SELECT DISTINCT scope FROM connector_bindings WHERE scope LIKE 'workspace:%'",
        ).fetchall()
        binding_workspace_scopes = [
            s["scope"] for s in binding_scopes if s["scope"].startswith("workspace:")
        ]

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + workspace_scopes + binding_workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes + binding_workspace_scopes,
        active_workspace_ids=active_workspace_ids,
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes,
        active_workspace_ids=active_workspace_ids,
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    all_bindings = connector_service.list_bindings(include_all_scopes=True)
    binding_workspace_ids = frozenset(
        b["scope"].split(":", 1)[1]
        for b in all_bindings
        if b.get("scope", "").startswith("workspace:")
    )
    all_active_workspace_ids = active_workspace_ids | binding_workspace_ids

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes,
        active_workspace_ids=all_active_workspace_ids,
    )
    active_workspace_ids = frozenset(w["id"] for w in workspaces)
    workspace_scopes = [f"workspace:{w['id']}" for w in workspaces]

    all_bindings = connector_service.list_bindings(scope=None, include_all_scopes=True)
    binding_workspace_ids = frozenset(
        b["scope"].split(":", 1)[1]
        for b in all_bindings
        if b.get("scope", "").startswith("workspace:")
    )
    all_active_workspace_ids = active_workspace_ids | binding_workspace_ids

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=session.get("role") == "admin",
        read_scopes=[f"user:{user_id}"] + workspace_scopes,
        write_scopes=[f"user:{user_id}"] + workspace_scopes,
        active_workspace_ids=all_active_workspace_ids,
    )
