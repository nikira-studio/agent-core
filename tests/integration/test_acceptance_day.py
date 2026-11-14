"""Acceptance: two agents work in one workspace in a day; each, in a fresh session, can answer
what the other did, what is still open, and recall the other's memory, from Core alone.

This is the common case: two coding agents in one workspace. Plain questions, planted facts, no model.
"""

from app.database import get_db
from app.services import agent_service, workspace_service

WS = "workspace:accept-a"
OTHER_WS = "workspace:accept-b"


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def _agent(owner_id, agent_id, scopes):
    _, key = agent_service.create_agent(
        agent_id=agent_id, display_name=agent_id, owner_user_id=owner_id, read_scopes=scopes, write_scopes=scopes
    )
    return key


def _session(client, key, scope):
    """A fresh session: the injected context, with its own execution id."""
    return _mcp(client, key, "session_context", memory_scope=scope, host_session_ref=f"fresh-{key[-6:]}")


def _day(client, key, scope, who, task, result, next_step, memory, *, finish=True):
    ctx = _session(client, key, scope)
    execution = ctx["execution_id"]
    _mcp(client, key, "activity_update", memory_scope=scope, task_description=task, status="active", execution_id=execution)
    _mcp(client, key, "activity_update", memory_scope=scope, task_note=f"{who} is partway through", execution_id=execution)
    _mcp(client, key, "memory_write", content=memory, memory_class="decision", scope=scope, topic=f"{who} decision", execution_id=execution)
    if finish:
        _mcp(
            client, key, "activity_update", memory_scope=scope, status="completed", task_result=result,
            handoff={"next_steps": [next_step]}, execution_id=execution,
        )
    return execution


def test_two_agents_in_one_workspace_know_what_each_other_did(test_client, admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("accept-a", "accept-a", owner_id)
    workspace_service.create_workspace("accept-b", "accept-b", owner_id)
    codex = _agent(owner_id, "codex-x", [WS, "agent:codex-x"])
    claude = _agent(owner_id, "claude-x", [WS, OTHER_WS, "agent:claude-x"])

    _day(test_client, codex, WS, "codex", "Rotate the build cache key", "Cache key rotated, commit c0dec0de",
         "Update the CI docs", "The build cache key is rotated monthly by the maintainer, never by CI.")
    # Claude Code is still working at the end of the day: its task is open.
    _day(test_client, claude, WS, "claude", "Migrate the importer", "", "", "The importer reads CSV only; JSON lands in 1.4.",
         finish=False)
    # Work in a different workspace must not leak into this one.
    _day(test_client, claude, OTHER_WS, "claude", "Unrelated website fix", "Fixed the footer", "none",
         "The footer links are generated, never hand-edited.")

    # Next morning, fresh sessions.
    claude_morning = _session(test_client, claude, WS)["context"]
    codex_morning = _session(test_client, codex, WS)["context"]

    # "What did codex finish yesterday, and what is left?" (asked by claude)
    assert "Cache key rotated, commit c0dec0de" in claude_morning
    assert "Update the CI docs" in claude_morning
    # "What is each agent working on right now?" (asked by codex)
    assert "Migrate the importer" in codex_morning and "claude-x" in codex_morning
    # Neither sees the other workspace's work.
    assert "Unrelated website fix" not in claude_morning and "Fixed the footer" not in codex_morning

    # "Recall a memory the other wrote" (each searches the shared workspace).
    found = _mcp(test_client, claude, "memory_search", query="build cache key rotated monthly", scope=WS)["records"]
    assert any("rotated monthly by the maintainer" in r["content"] for r in found)
    found = _mcp(test_client, codex, "memory_search", query="importer reads CSV", scope=WS)["records"]
    assert any("CSV only" in r["content"] for r in found)
    # A workspace an agent cannot read stays closed: codex has no access to the other one.
    denied = test_client.post(
        "/mcp", headers={"Authorization": f"Bearer {codex}"},
        json={"tool": "memory_search", "params": {"query": "footer", "scope": OTHER_WS}},
    )
    assert denied.status_code == 403
