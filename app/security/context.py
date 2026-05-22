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
    workspaces = workspace_service.list_accessible_workspaces(user_id, is_active=True)
    readable_workspace_ids = [
        w["id"] for w in workspaces if workspace_service.can_user_read_workspace(user_id, w["id"])
    ]
    writable_workspace_ids = [
        w["id"] for w in workspaces if workspace_service.can_user_write_workspace(user_id, w["id"])
    ]
    active_workspace_ids = frozenset(readable_workspace_ids)
    readable_workspace_scopes = [f"workspace:{wid}" for wid in readable_workspace_ids]
    writable_workspace_scopes = [f"workspace:{wid}" for wid in writable_workspace_ids]

    return RequestContext(
        actor_type="user",
        actor_id=user_id,
        user_id=user_id,
        is_admin=is_admin,
        read_scopes=[f"user:{user_id}"] + readable_workspace_scopes,
        write_scopes=[f"user:{user_id}"] + writable_workspace_scopes,
        active_workspace_ids=active_workspace_ids,
    )


def build_user_context_for_connectors(session: dict) -> RequestContext:
    # Connector workflows are intentionally not granted extra scope beyond the
    # normal user context. Bindings remain usable when their workspace is already
    # in scope for that user or when the request is made as an agent.
    return build_user_context(session)
