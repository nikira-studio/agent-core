"""Review blocker B5: every MCP tool gets the session context on an agent's first call, with no hook and no remembered step."""

import time

from app.database import get_db
from app.routes import mcp as mcp_routes
from app.services import agent_service, workspace_service

WS = "workspace:sc-a"


def _setup(agent_id="newcomer"):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    if not workspace_service.get_workspace_by_id("sc-a"):
        workspace_service.create_workspace("sc-a", "sc-a", owner)
    _, key = agent_service.create_agent(agent_id=agent_id, display_name=agent_id, owner_user_id=owner, read_scopes=[WS, f"agent:{agent_id}"], write_scopes=[WS, f"agent:{agent_id}"])
    return key


def _call(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]


def test_the_first_call_of_any_tool_carries_the_session_context_once(test_client, admin_token):
    key = _setup()
    first = _call(test_client, key, "memory_search", query="anything", scope=WS)
    ctx = first["session_context"]
    assert ctx["execution_id"] and "session context" in str(ctx).lower()
    assert "session_context_note" in first and "execution_id" in first["session_context_note"]
    second = _call(test_client, key, "memory_search", query="anything", scope=WS)
    assert "session_context" not in second
    # A different tool, same session: nothing more.
    assert "session_context" not in _call(test_client, key, "activity_list")


def test_a_session_that_already_started_gets_nothing_extra(test_client, admin_token):
    key = _setup("veteran")
    started = _call(test_client, key, "session_context", memory_scope=WS)
    assert "session_context" not in started  # the tool itself is not wrapped twice
    assert "session_context" not in _call(test_client, key, "memory_search", query="ordinary", scope=WS)


def test_a_returning_agent_after_a_long_gap_gets_it_again(test_client, admin_token, monkeypatch):
    key = _setup("sleeper")
    assert "session_context" in _call(test_client, key, "activity_list")
    assert "session_context" not in _call(test_client, key, "activity_list")
    real = time.time
    monkeypatch.setattr(mcp_routes.time, "time", lambda: real() + 3 * 3600)
    assert "session_context" in _call(test_client, key, "activity_list")


def test_keyword_search_finds_inflected_words_without_an_embedding_backend(test_client, admin_token):
    """A default install has no embeddings: 'invoice export' must find 'Invoice exports use ...', 'csv delimiter' 'delimited CSV'."""
    key = _setup("searcher")
    _call(test_client, key, "memory_write", content="Invoice exports use semicolon-delimited CSV, UTF-8 with a byte order mark.", memory_class="fact", scope=WS, topic="invoice-export-format")
    _call(test_client, key, "memory_write", content="The nightly backup runs at 02:00 and keeps seven copies on the second disk.", memory_class="fact", scope=WS, topic="backup-schedule")
    for query in ("invoice export", "csv delimiter", "exported invoices", "delimiters used for CSV exports"):
        found = _call(test_client, key, "memory_search", query=query, scope=WS)["records"]
        assert any("semicolon" in r["content"] for r in found), (query, found)
        assert not any("backup" in r["content"] for r in found), (query, found)  # no noise from the stem or the fallback
    assert _call(test_client, key, "memory_search", query="kubernetes ingress controller", scope=WS)["records"] == []
