from app.security.context import RequestContext
from app.security.effective_authority import EffectiveAuthority


def _authority() -> EffectiveAuthority:
    return EffectiveAuthority(
        RequestContext(
            actor_type="agent", actor_id="worker", agent_id="worker", user_id="owner",
            read_scopes=["agent:worker", "workspace:project"],
            write_scopes=["agent:worker"],
            active_workspace_ids=frozenset({"project"}),
        )
    )


def test_permanent_authority_requires_explicit_known_resource_operations():
    authority = _authority()
    assert authority.can("memory", "read", scope="workspace:project")
    assert not authority.can("memory", "write", scope="workspace:project")
    assert not authority.can("unknown", "read", scope="workspace:project")
    assert not authority.can("memory", "delete", scope="workspace:project")


def test_delegated_placeholder_fails_closed_until_grant_intersection_exists():
    authority = _authority()
    delegated = EffectiveAuthority(authority.context, grant_id="grant")
    assert not delegated.can("memory", "read", scope="workspace:project")
