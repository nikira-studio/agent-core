"""The generated agent files say only what is specific to the workspace, in about 300 words."""

from app.routes.integrations_page import (
    _build_agents_md,
    _build_claude_md,
    _build_instructions,
    _build_session_prompt,
)

BASE = "http://core.example.com"
USER = "user:alex"
AGENT = "agent:assistant"
WORKSPACE = "workspace:agent-core"


def _all(workspace=WORKSPACE):
    return {
        "claude": _build_claude_md(BASE, USER, workspace, AGENT, "Assistant", "agent-core"),
        "agents": _build_agents_md(BASE, USER, workspace, AGENT, "agent-core"),
        "claude_code": _build_instructions("claude_code", BASE, USER, workspace, AGENT, "Assistant", "Alex", "agent-core"),
        "codex": _build_instructions("codex", BASE, USER, workspace, AGENT, "Assistant", "Alex", "agent-core"),
        "session": _build_session_prompt("codex", BASE, USER, workspace, AGENT, "Assistant", "Alex", "agent-core"),
    }


def test_each_generated_file_is_short():
    for name, text in _all().items():
        assert len(text.split()) <= 400, (name, len(text.split()))


def test_each_file_carries_the_workspace_specifics_and_the_session_rhythm():
    for name, text in _all().items():
        assert f"`{WORKSPACE}`" in text and f"`{USER}`" in text and f"`{AGENT}`" in text, name
        for needle in ("session_context", "execution_id", "activity_update", "task_result", "needs_human", "memory_search", "credential_get", "credential_request"):
            assert needle in text, (name, needle)


def test_each_file_says_when_to_write_a_memory_and_when_not_to():
    for name, text in _all().items():
        assert "Write a memory only for something a future session cannot get from the code" in text, name
        assert "Do not write: that you finished, deployed or fixed something" in text, name


def test_nothing_the_server_already_says_is_repeated():
    """No tool catalogue, no class tutorial, no sync-cursor mechanics: the MCP server's own text carries them."""
    for name, text in _all().items():
        assert "Core MCP tools include" not in text and "memory_reanchor" not in text, name
        assert "workspace_sync_ack" not in text and "subject_anchor" not in text, name


def test_claude_code_gets_the_schema_loading_line_and_others_do_not():
    files = _all()
    assert "ToolSearch(" in files["claude"] and "Never put an API key in this file" in files["claude"]
    assert "ToolSearch(" not in files["agents"]


def test_with_no_workspace_the_files_use_the_private_scope():
    for name, text in _all(workspace=None).items():
        assert f"your private scope `{AGENT}`" in text and f"memory_scope: {AGENT}" in text, name


def test_with_no_workspace_the_agent_is_told_to_start_from_its_own_private_scope():
    """session_context serves an agent's own private scope, so a new agent with no workspace has a working start."""
    for name, text in _all(workspace=None).items():
        assert "`session_context`" in text and f"`memory_scope` `{AGENT}`" in text, name
        assert "`execution_id`" in text and "ask the owner for a workspace" in text, name
