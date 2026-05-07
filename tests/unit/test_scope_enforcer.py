import pytest
from app.security.scope_enforcer import ScopeEnforcer
from app.services import workspace_service


def test_can_write_user_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["user:admin"],
        write_scopes=["user:admin"],
        agent_id="testagent",
    )
    assert enforcer.can_write("user:admin") == True
    assert enforcer.can_write("user:other") == False


def test_can_write_shared_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["shared"],
        write_scopes=["shared"],
        agent_id="testagent",
    )
    assert enforcer.can_write("shared") == True


def test_can_read_user_scope():
    enforcer = ScopeEnforcer(
        read_scopes=["user:admin"],
        write_scopes=[],
        agent_id="testagent",
    )
    assert enforcer.can_read("user:admin") == True
    assert enforcer.can_read("user:other") == False


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
    assert enforcer.can_write("shared") == True


def test_agent_cannot_write_without_scope():
    enforcer = ScopeEnforcer(
        read_scopes=[],
        write_scopes=[],
        agent_id="testagent",
    )
    assert enforcer.can_write("user:admin") == False
    assert enforcer.can_read("user:admin") == False


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

    assert enforcer.can_read("workspace:demo") == True
    assert enforcer.can_write("workspace:demo") == True
    assert calls == []
