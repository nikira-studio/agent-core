from typing import Optional

from app.services import agent_service
from app.services import workspace_service
from app.config import settings


class ScopeEnforcer:
    def __init__(
        self,
        read_scopes: list[str],
        write_scopes: list[str],
        agent_id: Optional[str] = None,
        is_admin: bool = False,
        active_workspace_ids: Optional[frozenset[str]] = None,
    ):
        self.read_scopes = set(read_scopes)
        self.write_scopes = set(write_scopes)
        self.agent_id = agent_id
        self.is_admin = is_admin
        self.active_workspace_ids = active_workspace_ids
        self._workspace_status_cache: dict[str, bool] = {}

    def can_read(self, scope: str) -> bool:
        if self.is_admin:
            return True
        if scope.startswith("workspace:"):
            if not self._workspace_active(scope):
                return False
            return scope in self.read_scopes
        if scope == "shared":
            return "shared" in self.read_scopes
        return scope in self.read_scopes

    def can_write(self, scope: str) -> bool:
        if self.is_admin:
            return True
        if scope.startswith("workspace:"):
            if not self._workspace_active(scope):
                return False
            return scope in self.write_scopes
        if scope == "shared":
            return self._can_write_shared()
        return scope in self.write_scopes

    def _can_write_shared(self) -> bool:
        if "shared" in self.write_scopes:
            return True
        if self.agent_id and settings.shared_scope_agent_list:
            return self.agent_id in settings.shared_scope_agent_list
        return False

    def _workspace_active(self, scope: str) -> bool:
        workspace_id = scope.split(":", 1)[1] if ":" in scope else scope
        if self.active_workspace_ids is not None:
            return workspace_id in self.active_workspace_ids

        if workspace_id in self._workspace_status_cache:
            return self._workspace_status_cache[workspace_id]

        workspace = workspace_service.get_workspace_by_id(workspace_id)
        active = bool(workspace and workspace.get("is_active", False))
        self._workspace_status_cache[workspace_id] = active
        return active

    def filter_readable_scopes(self, scopes: list[str]) -> list[str]:
        return [s for s in scopes if self.can_read(s)]

    def filter_writable_scopes(self, scopes: list[str]) -> list[str]:
        return [s for s in scopes if self.can_write(s)]


def build_agent_context(agent: dict) -> "RequestContext":
    from app.security.context import RequestContext

    read_scopes = agent_service.parse_scopes(agent["read_scopes_json"])
    write_scopes = agent_service.parse_scopes(agent["write_scopes_json"])
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in read_scopes + write_scopes
        if scope.startswith("workspace:") and ":" in scope
    }

    return RequestContext(
        actor_type="agent",
        actor_id=agent["id"],
        user_id=agent.get("default_user_id"),
        agent_id=agent["id"],
        read_scopes=read_scopes,
        write_scopes=write_scopes,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
        is_admin=False,
    )
