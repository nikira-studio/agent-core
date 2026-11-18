"""The rule for "may this agent use this connection" as it was before 1.4.0, frozen.

Until 1.4.0 an agent could use a connection when the connection's scope was one of its read scopes, minus its own
person's personal scope unless the agent's ``inherit_user_bindings`` switch was on, and when it held the See or Run
permission. 1.4.0 replaces the single scope with an audience list; the upgrade step that adds it compares access under
this rule (before) with the new rule (after) and refuses to run if any agent's reach differs.

Do not change this module to follow later rules: it exists to say what the old rule was. It reads the same request
context the old enforcement read (``RequestContext``: read scopes, the inherit switch, the principal's scope).
"""

from __future__ import annotations

from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer


def connector_read_scopes(context: RequestContext) -> list[str]:
    """The scopes whose connections this agent may see and run."""
    if context.actor_type == "agent" and not context.inherit_user_bindings and context.principal_user_scope:
        return [s for s in context.read_scopes if s != context.principal_user_scope]
    return list(context.read_scopes)


def _enforcer(context: RequestContext) -> ScopeEnforcer:
    return ScopeEnforcer(
        connector_read_scopes(context),
        context.write_scopes,
        agent_id=context.agent_id,
        is_admin=context.is_admin,
        active_workspace_ids=context.active_workspace_ids,
    )


def _has(context: RequestContext, permission: str) -> bool:
    held = context.capabilities
    if held is None:
        return True
    return permission in held or (permission == "connectors_read" and "connectors_execute" in held)


def can_see(context: RequestContext, scope: str) -> bool:
    """Old ``EffectiveAuthority.can("connector", "read", scope=...)``."""
    return _has(context, "connectors_read") and _enforcer(context).can_read(scope)


def can_run(context: RequestContext, scope: str) -> bool:
    """Old ``EffectiveAuthority.can_binding_action(...)``: the Run permission and the same scope rule."""
    return _has(context, "connectors_execute") and can_see(context, scope)
