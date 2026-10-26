import importlib.util
import io
import json
from pathlib import Path

from app.database import get_db
from app.services import activity_service, agent_service, memory_service, workspace_service

SCOPE = "workspace:ctx-test"


def _setup(admin_token, roots=None):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        if roots is not None:
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('workspace_repo_roots', ?)",
                (json.dumps(roots),),
            )
            conn.commit()
    workspace_service.create_workspace("ctx-test", "Context Test", owner_id)
    workspace_service.create_workspace("ctx-other", "Other", owner_id)
    _, api_key = agent_service.create_agent(
        agent_id="ctx-agent", display_name="Context Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE, "workspace:ctx-other"], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {api_key}"}


def _context(client, headers, **body):
    response = client.post("/api/session-context", headers=headers, json=body)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def test_context_carries_rules_work_changes_and_index(test_client, admin_token):
    headers = _setup(admin_token)
    rule, _ = memory_service.write_memory("Never edit vendored code.", "decision", SCOPE, topic="rules")
    memory_service.set_pinned(rule["id"], True)
    decision, _ = memory_service.write_memory("Prefer SQLite for local state.", "decision", SCOPE, topic="storage")
    fact, _ = memory_service.write_memory("The API listens on 3500.", "fact", SCOPE, topic="ports")
    done = activity_service.create_activity(
        agent_id="other-agent", user_id="u", task_description="Ship the importer",
        memory_scope=SCOPE,
    )
    activity_service.update_activity(done["id"], status="completed", task_result="Importer shipped; next: docs.")
    handed = activity_service.create_activity(
        agent_id="other-agent", user_id="u", task_description="Review the migration",
        memory_scope=SCOPE,
    )
    activity_service.reassign_activity(handed["id"], "ctx-agent")

    data = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-1")
    text = data["context"]

    assert data["memory_scope"] == SCOPE and data["resolved_by"] == "requested"
    assert f"execution_id: {data['execution_id']}" in text
    assert "Never edit vendored code." in text  # pinned, in full
    assert handed["id"] in text and "(from other-agent)" in text
    assert "Ship the importer" in text and "Importer shipped; next: docs." in text
    assert decision["id"] in text and fact["id"] in text
    assert text.count(fact["id"]) == 1  # listed once, under the changes

    assert rule["id"] in data["memory_ids"] and decision["id"] in data["memory_ids"]
    assert len(text) <= 9000

    with get_db() as conn:
        audit = json.loads(conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'context_injected'"
        ).fetchone()[0])
        state = conn.execute(
            "SELECT acknowledged_sequence FROM execution_sync_state WHERE execution_id = ?",
            (data["execution_id"],),
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(sequence) FROM workspace_changes WHERE memory_scope = ?", (SCOPE,)
        ).fetchone()[0]
    assert set(data["memory_ids"]) == set(audit["memory_ids"])
    assert state == newest  # the injected context counts as delivered

    # A later sync on that execution returns only what happened afterwards.
    memory_service.write_memory("Later decision.", "decision", SCOPE, topic="later")
    sync = test_client.post("/api/workspace-sync", headers=headers, json={
        "memory_scope": SCOPE, "execution_id": data["execution_id"],
    }).json()["data"]
    assert [c["summary"]["topic"] for c in sync["memory_changes"]] == ["later"]

    # On a later session nothing is new, so both sit in the index, decisions first.
    activity_service.create_activity(agent_id="ctx-agent", user_id="u", task_description="t", memory_scope=SCOPE)
    later = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-0")["context"]
    index = later[later.index("## Key records"):]
    assert index.index(decision["id"]) < index.index(fact["id"])


def test_index_puts_records_in_current_use_before_untouched_old_ones(test_client, admin_token):
    headers = _setup(admin_token)
    old_decision, _ = memory_service.write_memory("Old decision nobody uses.", "decision", SCOPE, topic="old")
    used_fact, _ = memory_service.write_memory("Fact agents keep recalling.", "fact", SCOPE, topic="used")
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_recalled_at = '2026-02-01T00:00:00+00:00' WHERE id = ?",
            (old_decision["id"],),
        )
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (used_fact["id"],),
        )
        conn.commit()
    memory_service.record_recall([used_fact["id"]])
    activity_service.create_activity(agent_id="ctx-agent", user_id="u", task_description="t", memory_scope=SCOPE)

    text = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-later")["context"]
    index = text[text.index("## Key records"):]
    assert index.index(used_fact["id"]) < index.index(old_decision["id"])


def test_same_host_session_reuses_its_execution(test_client, admin_token):
    headers = _setup(admin_token)
    first = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-1")
    again = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-1", source="compact")
    other = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-2")
    assert again["execution_id"] == first["execution_id"] and again["execution_reused"]
    assert other["execution_id"] != first["execution_id"]


def test_scope_resolves_from_cwd_by_deepest_root(test_client, admin_token, tmp_path):
    outer, inner = tmp_path / "proj", tmp_path / "proj" / "sub"
    headers = _setup(admin_token, roots={"workspace:ctx-other": str(outer), SCOPE: str(inner)})
    assert _context(test_client, headers, cwd=str(inner / "app"))["memory_scope"] == SCOPE
    assert _context(test_client, headers, cwd=str(outer / "x"))["memory_scope"] == "workspace:ctx-other"
    unmatched = _context(test_client, headers, cwd=str(tmp_path / "elsewhere"))
    assert unmatched["memory_scope"] is None and unmatched["context"] == ""


def test_scope_the_agent_cannot_read_is_denied(test_client, admin_token):
    headers = _setup(admin_token)
    response = test_client.post(
        "/api/session-context", headers=headers, json={"memory_scope": "workspace:nope"}
    )
    assert response.status_code == 403


def test_budget_drops_whole_items_and_keeps_pinned_rules(test_client, admin_token):
    headers = _setup(admin_token)
    rule, _ = memory_service.write_memory("Pinned rule survives.", "decision", SCOPE, topic="rule")
    memory_service.set_pinned(rule["id"], True)
    for index in range(40):
        memory_service.write_memory(f"Decision {index}: " + "detail " * 40, "decision", SCOPE, topic=f"d{index}")
    data = _context(test_client, headers, memory_scope=SCOPE, max_chars=2500)
    assert len(data["context"]) <= 2500
    assert data["reductions"] > 0
    assert "Pinned rule survives." in data["context"]
    shown = [line for line in data["context"].splitlines() if line.startswith("- ") and " · d" in line]
    assert 0 < len(shown) < 40
    assert all(not line.endswith("detail") for line in shown)  # lines end whole or with an ellipsis


def test_mcp_session_context_is_never_offloaded(test_client, admin_token, monkeypatch):
    from app.config import settings

    headers = _setup(admin_token)
    for index in range(30):
        memory_service.write_memory(f"Fact {index}: " + "x" * 200, "fact", SCOPE, topic=f"f{index}")
    monkeypatch.setattr(settings, "TOOL_RESULT_SPILL_THRESHOLD", 500)
    response = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "session_context", "arguments": {"memory_scope": SCOPE}},
    })
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert "offloaded" not in payload
    assert payload["context"].startswith("# Agent Core session context")
    assert "memory_ids" not in payload


def test_memory_get_by_ids_hides_unreadable_records_and_counts_recall(test_client, admin_token):
    headers = _setup(admin_token)
    mine, _ = memory_service.write_memory("Readable.", "fact", SCOPE, topic="a")
    hidden, _ = memory_service.write_memory("Hidden.", "fact", "workspace:secret", topic="b")
    response = test_client.post("/mcp", headers=headers, json={
        "tool": "memory_get", "params": {"ids": [mine["id"], hidden["id"], "missing"]},
    })
    data = response.json()["data"]
    assert [r["id"] for r in data["records"]] == [mine["id"]]
    assert data["not_found"] == [hidden["id"], "missing"]
    with get_db() as conn:
        recall = conn.execute("SELECT recall_count FROM memory_records WHERE id = ?", (mine["id"],)).fetchone()[0]
        audit = json.loads(conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'memory_fetched'"
        ).fetchone()[0])
    assert recall == 1
    assert audit["ids"] == [mine["id"]] and audit["missing"] == 2

    bad = test_client.post("/mcp", headers=headers, json={"tool": "memory_get", "params": {"ids": "x"}})
    assert bad.status_code == 400


def _hook():
    path = Path(__file__).resolve().parents[2] / "integrations" / "claude-code" / "agent_core_hook.py"
    spec = importlib.util.spec_from_file_location("session_start_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_reads_connection_from_claude_mcp_config(tmp_path):
    hook = _hook()
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"agent-core": {
        "type": "http", "url": "https://core.example/mcp",
        "headers": {"Authorization": "Bearer key-1"},
    }}}))
    assert hook.resolve_connection(str(tmp_path / "repo"), env={}, home=str(tmp_path)) == (
        "https://core.example", "key-1",
    )
    env = {"AGENT_CORE_URL": "http://override", "AGENT_CORE_API_KEY": "key-2"}
    assert hook.resolve_connection("/x", env=env, home=str(tmp_path)) == ("http://override", "key-2")
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path / "none")) == (None, None)


def test_hook_never_sends_the_env_key_to_a_project_supplied_url(tmp_path):
    hook = _hook()
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"agent-core": {
        "url": "https://attacker.example/mcp",
    }}}))
    env = {"AGENT_CORE_API_KEY": "real-key"}
    assert hook.resolve_connection(str(repo), env=env, home=str(home)) == (None, None)
    # The user's own config may supply the URL for an env key.
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"agent-core": {
        "url": "https://core.example/mcp",
    }}}))
    assert hook.resolve_connection(str(repo), env=env, home=str(home)) == (
        "https://core.example", "real-key",
    )


def test_hook_exits_cleanly_on_malformed_config_and_events(tmp_path):
    hook = _hook()
    (tmp_path / ".claude.json").write_text("[1, 2]")
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"agent-core": {
        "url": "https://core.example/mcp", "headers": "nope"}}, "projects": []}))
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    for stdin in ("[]", '"text"', "{not json"):
        assert hook.main(stdin=io.StringIO(stdin), stdout=io.StringIO(), env={}) == 0


def test_hook_injects_context_skips_resume_and_never_fails(monkeypatch):
    hook = _hook()
    env = {"AGENT_CORE_URL": "http://core", "AGENT_CORE_API_KEY": "k"}
    sent = {}

    def fake_post(base_url, api_key, path, payload):
        assert path == "/api/session-context"
        sent.update(payload)
        return {"context": "# Agent Core session context: workspace:w"}

    monkeypatch.setattr(hook, "post", fake_post)
    out = io.StringIO()
    event = {"session_id": "s1", "cwd": "/repo", "source": "startup"}
    assert hook.main(io.StringIO(json.dumps(event)), out, env) == 0
    result = json.loads(out.getvalue())
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert result["hookSpecificOutput"]["additionalContext"].startswith("# Agent Core")
    assert sent == {"cwd": "/repo", "host_session_ref": "s1", "source": "startup", "max_chars": 9000}

    out = io.StringIO()
    hook.main(io.StringIO(json.dumps({**event, "source": "resume"})), out, env)
    assert out.getvalue() == ""

    def failing(*_):
        raise OSError("connection refused")

    monkeypatch.setattr(hook, "post", failing)
    out = io.StringIO()
    assert hook.main(io.StringIO(json.dumps(event)), out, env) == 0
    assert "unavailable" in json.loads(out.getvalue())["systemMessage"]


def test_hook_script_is_served_publicly(test_client):
    response = test_client.get("/integrations/hooks/claude-code/agent_core_hook.py")
    assert response.status_code == 200
    assert "def resolve_connection" in response.text
    assert test_client.get("/integrations/hooks/../../app/config.py").status_code == 404


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def test_handoff_is_recorded_and_shown_to_the_next_session(test_client, admin_token):
    headers = _setup(admin_token)
    start = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-1")
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active",
         task_description="Wire the importer", execution_id=start["execution_id"])
    closed = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="completed",
                  task_result="Importer wired.", handoff={
                      "completed": "Importer wired to the queue.",
                      "next_steps": ["Add retry on 429"], "open_questions": ["Batch size?"],
                      "needs_human": "Approve the vendor contract.",
                  }).json()["data"]
    assert "warnings" not in closed
    activity_id = closed["activity"]["id"]

    text = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-2")["context"]
    assert "next steps: Add retry on 429" in text and "open questions: Batch size?" in text
    assert "## Waiting on a person" in text and "Approve the vendor contract." in text

    # The Activity page lists it with a control to resolve it.
    from app.services.auth_service import create_session
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    test_client.cookies.set("session_token", create_session(owner_id, channel="dashboard")["session_id"])
    page = test_client.get("/activity").text
    assert "Waiting on you" in page and "Approve the vendor contract." in page
    assert f"data-handoff-resolve='{activity_id}'" in page
    test_client.cookies.clear()

    # Only a person resolves it; then it stops appearing.
    assert test_client.post(f"/api/activity/{activity_id}/handoff/resolve", headers=headers).status_code == 403
    test_client.cookies.set("session_token", create_session(owner_id, channel="dashboard")["session_id"])
    assert test_client.post(f"/api/activity/{activity_id}/handoff/resolve").status_code == 200  # the button's call
    assert "Waiting on you" not in test_client.get("/activity").text
    test_client.cookies.clear()
    text = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-3")["context"]
    assert "Approve the vendor contract." not in text


def test_closing_without_handoff_warns_and_bad_handoff_is_rejected(test_client, admin_token):
    headers = _setup(admin_token)
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active", task_description="t")
    bad = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, handoff={"nextsteps": ["x"]})
    assert bad.status_code == 400 and "unknown handoff fields" in bad.json()["error"]["message"]
    closed = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="completed",
                  task_result="done").json()["data"]
    assert closed["warnings"][0]["code"] == "HANDOFF_MISSING"


def test_stop_check_asks_once_per_activity(test_client, admin_token):
    headers = _setup(admin_token)

    def check(ref="s-1"):
        return test_client.post("/api/session-context/handoff-check", headers=headers,
                                json={"host_session_ref": ref}).json()["data"]

    assert check() == {"nudge": False}  # no session yet
    start = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-1")
    assert check() == {"nudge": False}  # no open activity
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active",
         task_description="Long task", execution_id=start["execution_id"])
    first = check()
    assert first["nudge"] and "handoff=" in first["reason"]
    assert check() == {"nudge": False}  # never twice for the same activity
    assert check("other-session") == {"nudge": False}

    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="completed", task_result="x")
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active", task_description="Next")
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE,
         handoff={"next_steps": ["keep going"]})
    assert check() == {"nudge": False}  # already has a handoff


def test_stop_hook_blocks_only_on_a_nudge_and_never_loops(monkeypatch):
    hook = _hook()
    env = {"AGENT_CORE_URL": "http://core", "AGENT_CORE_API_KEY": "k"}
    monkeypatch.setattr(hook, "post", lambda *a: {"nudge": True, "reason": "record a handoff"})
    event = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/repo"}

    out = io.StringIO()
    hook.main(io.StringIO(json.dumps(event)), out, env)
    assert json.loads(out.getvalue()) == {"decision": "block", "reason": "record a handoff"}

    for extra, extra_env in (({"stop_hook_active": True}, {}), ({}, {"AGENT_CORE_HANDOFF_NUDGE": "0"})):
        out = io.StringIO()
        hook.main(io.StringIO(json.dumps({**event, **extra})), out, {**env, **extra_env})
        assert out.getvalue() == ""

    def failing(*_):
        raise OSError("down")

    monkeypatch.setattr(hook, "post", failing)
    out = io.StringIO()
    assert hook.main(io.StringIO(json.dumps(event)), out, env) == 0 and out.getvalue() == ""


def test_activity_and_audit_pagination_escape_query_values(test_client, admin_token):
    from app.services.auth_service import create_session

    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    test_client.cookies.set("session_token", create_session(owner_id, channel="dashboard")["session_id"])
    payload = "'><img src=x onerror=alert(1)>"
    activity = test_client.get("/activity", params={"status": payload}).text
    audit = test_client.get("/audit", params={"resource_type": payload}).text
    test_client.cookies.clear()
    assert "<img src=x" not in activity and "<img src=x" not in audit


def test_many_pinned_rules_cannot_push_out_waiting_work(test_client, admin_token):
    headers = _setup(admin_token)
    for index in range(10):  # the default pinned limit
        rule, _ = memory_service.write_memory(f"Rule {index}: " + "always " * 300, "decision", SCOPE, topic=f"r{index}")
        memory_service.set_pinned(rule["id"], True)
    handed = activity_service.create_activity(
        agent_id="other-agent", user_id="u", task_description="Finish the migration", memory_scope=SCOPE,
    )
    activity_service.reassign_activity(handed["id"], "ctx-agent")
    text = _context(test_client, headers, memory_scope=SCOPE, max_chars=2000)["context"]
    assert len(text) <= 2000
    assert "Finish the migration" in text


def test_reused_execution_keeps_its_cursor(test_client, admin_token):
    headers = _setup(admin_token)
    first = _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-r")
    memory_service.write_memory("Written mid-session by someone else.", "fact", SCOPE, topic="mid")
    _context(test_client, headers, memory_scope=SCOPE, host_session_ref="s-r", source="compact")
    sync = test_client.post("/api/workspace-sync", headers=headers, json={
        "memory_scope": SCOPE, "execution_id": first["execution_id"],
    }).json()["data"]
    assert [c["summary"]["topic"] for c in sync["memory_changes"]] == ["mid"]


def test_resolved_handoff_request_stays_resolved_unless_it_changes(test_client, admin_token):
    activity = activity_service.create_activity(
        agent_id="a", user_id="u", task_description="t", memory_scope=SCOPE,
    )
    activity_service.set_handoff(activity["id"], {"needs_human": "Approve it."})
    assert activity_service.resolve_handoff(activity["id"])
    activity_service.set_handoff(activity["id"], {"needs_human": "Approve it.", "next_steps": ["x"]})
    assert activity_service.get_activity(activity["id"])["handoff_resolved_at"]
    activity_service.set_handoff(activity["id"], {"needs_human": "Sign the contract."})
    assert not activity_service.get_activity(activity["id"])["handoff_resolved_at"]
