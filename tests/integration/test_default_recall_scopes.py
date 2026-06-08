"""Phase 1 — Default Recall Scopes: unscoped recall fans only the default set;
scoped reads still reach the full read_scopes on demand."""

import json

import pytest

from app.database import get_db
from app.services import agent_service, memory_service
from app.security.scope_enforcer import build_agent_context


@pytest.fixture
def owner(admin_token):
    # admin_token ensures a clean DB + registered admin user; return its real id
    # to satisfy the agents.owner_user_id FK. Also create the workspaces the tests
    # reference, since can_read gates workspace scopes on the workspace existing
    # and being accessible to the agent's owner.
    from app.services import workspace_service

    with get_db() as conn:
        owner_id = conn.execute(
            "SELECT id FROM users WHERE email = ?", ("admin@test.local",)
        ).fetchone()["id"]
    for wid in ("personal", "other", "secret"):
        workspace_service.create_workspace(
            workspace_id=wid, name=wid.title(), owner_user_id=owner_id
        )
    return owner_id


def _seed(scope, token):
    rec, err = memory_service.write_memory(
        content=f"phaseone {token} record in {scope}",
        memory_class="fact",
        scope=scope,
        topic=f"t-{token}",
    )
    assert err is None
    return rec


class TestDefaultRecallService:
    def test_create_constrains_to_subset_and_own_scope(self, owner):
        agent, _ = agent_service.create_agent(
            agent_id="recall-svc",
            display_name="x",
            owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
            default_recall_scopes=["workspace:personal", "workspace:notreadable"],
        )
        stored = json.loads(agent["default_recall_scopes_json"])
        # own scope always present; non-readable dropped; readable kept.
        assert "agent:recall-svc" in stored
        assert "workspace:personal" in stored
        assert "workspace:notreadable" not in stored
        assert "workspace:other" not in stored

    def test_create_none_stores_null(self, owner):
        agent, _ = agent_service.create_agent(
            agent_id="recall-null", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal"],
        )
        assert agent["default_recall_scopes_json"] is None

    def test_context_null_fans_all_read(self, owner):
        agent, _ = agent_service.create_agent(
            agent_id="recall-ctxnull", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
        )
        ctx = build_agent_context(agent_service.get_agent_by_id("recall-ctxnull"))
        assert set(ctx.default_recall_scopes) == set(ctx.read_scopes)

    def test_context_set_narrows_with_own_scope(self, owner):
        agent_service.create_agent(
            agent_id="recall-ctxset", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
            default_recall_scopes=["workspace:personal"],
        )
        ctx = build_agent_context(agent_service.get_agent_by_id("recall-ctxset"))
        assert set(ctx.default_recall_scopes) == {"agent:recall-ctxset", "workspace:personal"}
        assert "workspace:other" in ctx.read_scopes  # full read access intact

    def test_update_clear_resets_to_null(self, owner):
        agent_service.create_agent(
            agent_id="recall-clear", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
            default_recall_scopes=["workspace:personal"],
        )
        agent_service.update_agent("recall-clear", default_recall_scopes=None)
        assert agent_service.get_agent_by_id("recall-clear")["default_recall_scopes_json"] is None

    def test_update_read_change_prunes_default(self, owner):
        agent_service.create_agent(
            agent_id="recall-prune", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
            default_recall_scopes=["workspace:personal", "workspace:other"],
        )
        # Drop workspace:other from read_scopes; it must be pruned from the default.
        agent_service.update_agent("recall-prune", read_scopes=["workspace:personal"])
        stored = json.loads(
            agent_service.get_agent_by_id("recall-prune")["default_recall_scopes_json"]
        )
        assert "workspace:other" not in stored
        assert "workspace:personal" in stored


class TestDefaultRecallMCP:
    def _agent_key(self, owner):
        agent, key = agent_service.create_agent(
            agent_id="recall-mcp",
            display_name="x",
            owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
            default_recall_scopes=["workspace:personal"],
        )
        return key

    def test_unscoped_search_excludes_offdesk_scope(self, test_client, owner):
        key = self._agent_key(owner)
        _seed("agent:recall-mcp", "own")
        _seed("workspace:personal", "personal")
        _seed("workspace:other", "other")

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_search", "params": {"query": "phaseone record"}},
        )
        assert r.status_code == 200, r.text
        scopes = {rec["scope"] for rec in r.json()["data"]["records"]}
        assert "workspace:other" not in scopes
        assert "workspace:personal" in scopes or "agent:recall-mcp" in scopes

    def test_scoped_search_reaches_offdesk_on_demand(self, test_client, owner):
        key = self._agent_key(owner)
        _seed("workspace:other", "other")

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_search", "params": {"query": "phaseone record", "scope": "workspace:other"}},
        )
        assert r.status_code == 200, r.text
        scopes = {rec["scope"] for rec in r.json()["data"]["records"]}
        assert scopes == {"workspace:other"}

    def test_scoped_search_denied_for_unreadable_scope(self, test_client, owner):
        key = self._agent_key(owner)
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_search", "params": {"query": "phaseone record", "scope": "workspace:secret"}},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "SCOPE_DENIED"

    def test_unscoped_memory_get_excludes_offdesk_but_scoped_reaches_it(self, test_client, owner):
        key = self._agent_key(owner)
        _seed("workspace:personal", "personal")
        _seed("workspace:other", "other")

        unscoped = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_get", "params": {"view": "full"}},
        )
        scopes = {rec["scope"] for rec in unscoped.json()["data"]["records"]}
        assert "workspace:other" not in scopes

        scoped = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_get", "params": {"scope": "workspace:other", "view": "full"}},
        )
        scoped_scopes = {rec["scope"] for rec in scoped.json()["data"]["records"]}
        assert scoped_scopes == {"workspace:other"}

    def test_null_default_is_backcompat_all_read(self, test_client, owner):
        # An agent with NULL default still fans all read_scopes (Option A).
        _agent, key = agent_service.create_agent(
            agent_id="recall-bc", display_name="x", owner_user_id=owner,
            read_scopes=["workspace:personal", "workspace:other"],
        )
        _seed("workspace:other", "other")
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json={"tool": "memory_search", "params": {"query": "phaseone record"}},
        )
        scopes = {rec["scope"] for rec in r.json()["data"]["records"]}
        assert "workspace:other" in scopes
