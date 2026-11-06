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
    # Scopes an UNSCOPED memory_search/memory_get recalls from by default. A
    # subset of read_scopes (scoped reads still use the full read_scopes). For
    # users this equals read_scopes; for agents it can be narrowed.
    default_recall_scopes: list[str] = field(default_factory=list)
    active_workspace_ids: frozenset[str] = field(default_factory=frozenset)
    is_admin: bool = False
    # A coarse, standing ceiling over operation classes. Scopes still decide
    # which records a capability can reach.
    capabilities: Optional[frozenset[str]] = None
    # True only for a person acting in their own browser (the dashboard cookie
    # session). A service action that waits for approval does not hold them;
    # it holds every key, token and script.
    browser_session: bool = False
    # An agent acts for a person and reads that person's own scope for memory. Running
    # the services bound in that scope is a separate, per-agent setting, off unless an
    # admin turned it on; principal_user_scope names the scope it covers.
    principal_user_scope: Optional[str] = None
    inherit_user_bindings: bool = True

    def connector_read_scopes(self) -> list[str]:
        """The scopes whose bindings this caller may see and run."""
        if self.actor_type == "agent" and not self.inherit_user_bindings and self.principal_user_scope:
            return [s for s in self.read_scopes if s != self.principal_user_scope]
        return list(self.read_scopes)


def build_user_context(session: dict) -> RequestContext:
    from app.services import workspace_service

    user_id = session["user_id"]
    is_admin = session.get("role") == "admin"
    workspaces = workspace_service.list_accessible_workspaces(user_id, is_active=True)
    readable, writable = workspace_service.get_workspace_authority(
        user_id, [w["id"] for w in workspaces]
    )
    readable_workspace_ids = [w["id"] for w in workspaces if w["id"] in readable]
    writable_workspace_ids = [w["id"] for w in workspaces if w["id"] in writable]
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
        default_recall_scopes=[f"user:{user_id}"] + readable_workspace_scopes,
        active_workspace_ids=active_workspace_ids,
        capabilities=frozenset(
            {
                "memory",
                "coordination",
                "credentials",
                "connectors_read",
                "connectors_execute",
            }
        ),
    )


def build_user_context_for_connectors(session: dict) -> RequestContext:
    # Connector workflows are intentionally not granted extra scope beyond the
    # normal user context. Bindings remain usable when their workspace is already
    # in scope for that user or when the request is made as an agent.
    return build_user_context(session)
