from typing import Optional, TYPE_CHECKING

from app.services import agent_service
from app.services import workspace_service
from app.config import settings

if TYPE_CHECKING:
    from app.security.context import RequestContext


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


def build_agent_context(agent: dict) -> "RequestContext":
    from app.security.context import RequestContext
    from app.services import auth_service

    stored_read_scopes = agent_service.parse_scopes(agent["read_scopes_json"])
    stored_write_scopes = agent_service.parse_scopes(agent["write_scopes_json"])

    # default_user_id is the principal whose workspace authority the agent may
    # exercise. owner_user_id manages the agent record; it is only a fallback
    # for older rows without a selected default user.
    user_id = agent.get("default_user_id") or agent.get("owner_user_id")
    principal = auth_service.get_user_by_id(user_id) if user_id else None
    principal_is_active = bool(principal and principal.get("is_active", True))

    # Default recall set: NULL column = Option A (fan all read_scopes). When set,
    # constrain to read_scopes and always include the agent's own scope, so an
    # unscoped recall can never be stranded from its own memory or drift past
    # read access regardless of how the row was edited.
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in stored_read_scopes + stored_write_scopes
        if scope.startswith("workspace:") and ":" in scope
    }
    readable_workspace_ids, writable_workspace_ids = (
        workspace_service.get_workspace_authority(user_id, workspace_ids)
        if principal_is_active
        else (frozenset(), frozenset())
    )

    def attenuate(scopes: list[str], allowed_workspace_ids: frozenset[str]) -> list[str]:
        result = []
        for scope in scopes:
            if scope.startswith("workspace:"):
                workspace_id = scope.split(":", 1)[1]
                if workspace_id not in allowed_workspace_ids:
                    continue
            # A disabled default user cannot lend access to their private data.
            if scope == f"user:{user_id}" and not principal_is_active:
                continue
            result.append(scope)
        return result

    read_scopes = attenuate(stored_read_scopes, readable_workspace_ids)
    write_scopes = attenuate(stored_write_scopes, writable_workspace_ids)
    read_set = set(read_scopes)
    own_scope = f"agent:{agent['id']}"
    recall_json = agent.get("default_recall_scopes_json")
    if recall_json:
        default_recall_scopes = [
            s for s in agent_service.parse_scopes(recall_json) if s in read_set
        ]
    else:
        default_recall_scopes = list(read_scopes)
    if own_scope in read_set and own_scope not in default_recall_scopes:
        default_recall_scopes.insert(0, own_scope)

    active_workspace_ids = readable_workspace_ids | writable_workspace_ids

    return RequestContext(
        actor_type="agent",
        actor_id=agent["id"],
        user_id=user_id,
        agent_id=agent["id"],
        read_scopes=read_scopes,
        write_scopes=write_scopes,
        default_recall_scopes=default_recall_scopes,
        active_workspace_ids=active_workspace_ids,
        is_admin=False,
    )
