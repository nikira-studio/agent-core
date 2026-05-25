from app.security.scope_enforcer import ScopeEnforcer
from app.services import workspace_service


def test_can_write_user_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["user:admin"],
        write_scopes=["user:admin"],
        agent_id="testagent",
    )
    assert enforcer.can_write("user:admin") is True
    assert enforcer.can_write("user:other") is False


def test_can_write_shared_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["shared"],
        write_scopes=["shared"],
        agent_id="testagent",
    )
    assert enforcer.can_write("shared") is True


def test_can_read_user_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["user:admin"],
        write_scopes=[],
        agent_id="testagent",
    )
    assert enforcer.can_read("user:admin") is True
    assert enforcer.can_read("user:other") is False


def test_filter_readable_scopes():
    enforcer = ScopeEnforcer(
        read_scopes=["user:admin", "workspace:myworkspace"],
        write_scopes=[],
        agent_id="testagent",
    )
    result = enforcer.filter_readable_scopes(["user:admin", "user:other"])
    assert "user:admin" in result
    assert "user:other" not in result


def test_shared_scope_agents_can_write_shared():
    enforcer = ScopeEnforcer(
        read_scopes=[],
        write_scopes=["shared"],
        agent_id="shared-agent",
    )
    assert enforcer.can_write("shared") is True


def test_agent_cannot_write_without_scope():
    enforcer = ScopeEnforcer(
        read_scopes=[],
        write_scopes=[],
        agent_id="testagent",
    )
    assert enforcer.can_write("user:admin") is False
    assert enforcer.can_read("user:admin") is False


def test_workspace_scope_cache_avoids_repeated_db_lookups(monkeypatch):
    calls = []

    def fake_get_workspace_by_id(workspace_id):
        calls.append(workspace_id)
        return {"id": workspace_id, "is_active": True}

    monkeypatch.setattr(workspace_service, "get_workspace_by_id", fake_get_workspace_by_id)
    enforcer = ScopeEnforcer(
        read_scopes=["workspace:demo"],
        write_scopes=["workspace:demo"],
        agent_id="testagent",
        active_workspace_ids=frozenset({"demo"}),
    )

    assert enforcer.can_read("workspace:demo") is True
    assert enforcer.can_write("workspace:demo") is True
    assert calls == []


def test_inactive_workspace_is_rejected_via_db_fallback(monkeypatch):
    def fake_get_workspace_by_id(workspace_id):
        return {"id": workspace_id, "is_active": False}

    monkeypatch.setattr(workspace_service, "get_workspace_by_id", fake_get_workspace_by_id)
    enforcer = ScopeEnforcer(
        read_scopes=["workspace:inactive"],
        write_scopes=["workspace:inactive"],
        agent_id="testagent",
        active_workspace_ids=None,
    )

    assert enforcer.can_read("workspace:inactive") is False
    assert enforcer.can_write("workspace:inactive") is False
