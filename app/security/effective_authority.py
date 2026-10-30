"""Central, transport-neutral authorization representation.

``RequestContext`` remains the authenticated identity and permanent scope
snapshot.  This wrapper is the only object new protected operations should
accept: it makes the requested resource operation explicit.
"""

from dataclasses import dataclass
from typing import Optional

from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer


# Closed v1 vocabulary. Adding a protected resource requires adding it here and
# explicitly deciding whether its operation is read- or write-like.
RESOURCE_OPERATIONS: dict[str, frozenset[str]] = {
    "memory": frozenset({"read", "write"}),
    "activity": frozenset({"read", "create", "update", "cancel"}),
    "connector": frozenset({"read", "execute"}),
    "credential": frozenset({"read", "write", "reference", "reveal"}),
}

ALL_CAPABILITIES = frozenset({"memory", "coordination", "credentials", "connectors_read", "connectors_execute", "credentials_resolve"})
RESOURCE_CAPABILITIES: dict[tuple[str, str], str] = {
    ("memory", "read"): "memory", ("memory", "write"): "memory",
    ("activity", "read"): "coordination", ("activity", "create"): "coordination",
    ("activity", "update"): "coordination", ("activity", "cancel"): "coordination",
    ("credential", "read"): "credentials", ("credential", "write"): "credentials",
    ("credential", "reference"): "credentials", ("credential", "reveal"): "credentials",
    ("connector", "read"): "connectors_read", ("connector", "execute"): "connectors_execute",
}

WRITE_OPERATIONS = frozenset({"write", "create", "update", "cancel", "execute", "reveal"})


@dataclass(frozen=True)
class EffectiveAuthority:
    """Effective authority for one request: the caller's permanent key or session."""

    context: RequestContext

    @property
    def actor_type(self) -> str:
        return self.context.actor_type

    @property
    def actor_id(self) -> str:
        return self.context.actor_id

    @property
    def user_id(self) -> Optional[str]:
        return self.context.user_id

    @property
    def agent_id(self) -> Optional[str]:
        return self.context.agent_id

    @property
    def read_scopes(self) -> list[str]:
        return self.context.read_scopes

    @property
    def write_scopes(self) -> list[str]:
        return self.context.write_scopes

    @property
    def default_recall_scopes(self) -> list[str]:
        return self.context.default_recall_scopes

    @property
    def active_workspace_ids(self) -> frozenset[str]:
        return self.context.active_workspace_ids

    @property
    def is_admin(self) -> bool:
        return self.context.is_admin

    @property
    def capabilities(self) -> frozenset[str]:
        # Older RequestContext construction in integrations did not include
        # capabilities. Preserve its full-authority behavior. An explicit empty
        # list on an agent remains an empty capability set.
        return ALL_CAPABILITIES if self.context.capabilities is None else self.context.capabilities

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities or (
            capability == "connectors_read" and "connectors_execute" in self.capabilities
        )

    def required_capability(self, resource_type: str, operation: str) -> Optional[str]:
        return RESOURCE_CAPABILITIES.get((resource_type, operation))

    def can(self, resource_type: str, operation: str, *, scope: Optional[str] = None) -> bool:
        """Test a closed resource operation against the effective authority."""
        if operation not in RESOURCE_OPERATIONS.get(resource_type, frozenset()):
            return False
        required = self.required_capability(resource_type, operation)
        if required and not self.has_capability(required):
            return False
        if scope is None:
            return self.context.is_admin
        enforcer = ScopeEnforcer(
            self.context.read_scopes,
            self.context.write_scopes,
            agent_id=self.context.agent_id,
            is_admin=self.context.is_admin,
            active_workspace_ids=self.context.active_workspace_ids,
        )
        if operation in WRITE_OPERATIONS:
            return enforcer.can_write(scope)
        return enforcer.can_read(scope)

    def filter_scopes(self, resource_type: str, operation: str, scopes: list[str]) -> list[str]:
        return [scope for scope in scopes if self.can(resource_type, operation, scope=scope)]

    def can_binding_action(self, binding_id: str, action: str, *, scope: str) -> bool:
        if not self.has_capability("connectors_execute"):
            return False
        return self.can("connector", "read", scope=scope)

    def safe_summary(self) -> dict:
        """Return only information safe for an authenticated caller to inspect."""
        return {
            "authenticated_actor": {"type": self.context.actor_type, "id": self.context.actor_id},
            "executor_agent_id": self.context.agent_id,
            "principal_user_id": self.context.user_id,
            # Compatibility: delegated authority was removed (2026-10-25), so
            # request authority is always permanent. Clients written against
            # the earlier contract, OpenAgents' human sign-in among them,
            # refuse a response without this field.
            "authorization_mode": "permanent",
            "effective_read_scopes": self.context.read_scopes,
            "effective_write_scopes": self.context.write_scopes,
            "active_workspace_ids": sorted(self.context.active_workspace_ids),
            "capabilities": sorted(self.capabilities),
        }

    def safe_attribution(self) -> dict:
        return {
            "principal_user_id": self.context.user_id,
            "executor_agent_id": self.context.agent_id,
        }


def permanent_authority(context: RequestContext) -> EffectiveAuthority:
    return EffectiveAuthority(context=context)


@dataclass(frozen=True)
class SystemAuthority:
    """Explicit authority for non-request maintenance paths."""

    reason: str
    actor_id: str = "system"
    actor_type: str = "system"

    def can_binding_action(self, binding_id: str, action: str, *, scope: str) -> bool:
        return bool(self.reason and binding_id and action and scope)


def system_authority(reason: str) -> SystemAuthority:
    if not reason.strip():
        raise ValueError("System authority requires an audit reason")
    return SystemAuthority(reason=reason.strip())
