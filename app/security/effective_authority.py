"""Central, transport-neutral authorization representation.

``RequestContext`` remains the authenticated identity and permanent scope
snapshot.  This wrapper is the only object new protected operations should
accept: it makes the requested resource operation explicit and leaves a single
place to intersect a future delegated grant without teaching every route about
grant storage or transport headers.
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
    "briefing": frozenset({"read", "create"}),
    "connector": frozenset({"read", "execute"}),
    "credential": frozenset({"read", "write", "reference", "reveal"}),
}

WRITE_OPERATIONS = frozenset({"write", "create", "update", "cancel", "execute", "reveal"})


@dataclass(frozen=True)
class EffectiveAuthority:
    """Effective authority for one request, currently permanent-only.

    The optional delegation fields are intentionally descriptive until grant
    enforcement is introduced.  They make the future intersection explicit
    without silently changing any existing permanent-key behavior.
    """

    context: RequestContext
    grant_id: Optional[str] = None
    principal_user_id: Optional[str] = None
    issuer_actor_id: Optional[str] = None
    coordinator_agent_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.principal_user_id is None:
            object.__setattr__(self, "principal_user_id", self.context.user_id)

    def __getattr__(self, name):
        """Compatibility bridge while routes migrate from RequestContext."""
        return getattr(self.context, name)

    @property
    def is_delegated(self) -> bool:
        return self.grant_id is not None

    def can(self, resource_type: str, operation: str, *, scope: Optional[str] = None) -> bool:
        """Test a closed resource operation against the effective authority.

        Scope is an address, never sufficient delegated authority on its own.
        Delegation is deliberately unsupported until the grant service can
        intersect resource permissions, so a grant-bearing authority fails
        closed rather than falling back to permanent agent scopes.
        """
        if operation not in RESOURCE_OPERATIONS.get(resource_type, frozenset()):
            return False
        if self.is_delegated:
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

    def safe_summary(self) -> dict:
        """Return only information safe for an authenticated caller to inspect."""
        return {
            "authenticated_actor": {"type": self.context.actor_type, "id": self.context.actor_id},
            "executor_agent_id": self.context.agent_id,
            "principal_user_id": self.principal_user_id,
            "authorization_mode": "delegated" if self.is_delegated else "permanent",
            "grant_id": self.grant_id,
            "effective_read_scopes": self.context.read_scopes,
            "effective_write_scopes": self.context.write_scopes,
            "active_workspace_ids": sorted(self.context.active_workspace_ids),
        }


def permanent_authority(context: RequestContext) -> EffectiveAuthority:
    return EffectiveAuthority(context=context)
