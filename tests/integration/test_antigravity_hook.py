"""The Antigravity hook (SessionStart and Stop): reads Antigravity's own MCP config, sends what the Codex and
Claude Code hooks send, speaks Antigravity's camelCase JSON, and never breaks a session."""

import importlib.util
import io
import json
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {"AGENT_CORE_URL": "http://core", "AGENT_CORE_API_KEY": "k"}


def _load(*parts):
    path = ROOT.joinpath("integrations", *parts)
    spec = importlib.util.spec_from_file_location("hook_" + "_".join(parts).replace(".", "_").replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook():
    return _load("antigravity", "agent_core_hook.py")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _config(home, entry):
    _write(home / ".gemini" / "config" / "mcp_config.json", json.dumps({"mcpServers": {"agent-core": entry}}))


def _run(hook, event, env=None):
    out = io.StringIO()
    assert hook.main(io.StringIO(json.dumps(event)), out, ENV if env is None else env) == 0
    return out.getvalue()


# --- connection ---------------------------------------------------------------------------------------------------


def test_reads_url_and_key_from_the_users_mcp_config_under_either_url_key(tmp_path):
    hook = _hook()
    _config(tmp_path, {"url": "https://core.example/mcp", "headers": {"Authorization": "Bearer key-1"}})
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == ("https://core.example", "key-1")
    _config(tmp_path, {"serverUrl": "https://core.example/mcp", "headers": {"authorization": "Bearer key-2"}})
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == ("https://core.example", "key-2")
    # the environment overrides the file, and a key alone pairs with the file's URL
    assert hook.resolve_connection("/x", env={"AGENT_CORE_URL": "http://o", "AGENT_CORE_API_KEY": "k3"}, home=str(tmp_path)) == ("http://o", "k3")
    _config(tmp_path, {"url": "https://core.example/mcp"})
    assert hook.resolve_connection("/x", env={"AGENT_CORE_API_KEY": "k4"}, home=str(tmp_path)) == ("https://core.example", "k4")
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path / "none")) == (None, None)
    assert hook.resolve_connection("/x", env={"AGENT_CORE_MCP_NAME": "other"}, home=str(tmp_path)) == (None, None)


def test_a_workspaces_own_mcp_config_is_never_read(tmp_path):
    hook = _hook()
    home, repo = tmp_path / "home", tmp_path / "work" / "repo"
    for folder in (tmp_path / "work", repo):
        _write(
            folder / ".agents" / "mcp_config.json",
            json.dumps({"mcpServers": {"agent-core": {"url": "https://attacker.example/mcp", "headers": {"Authorization": "Bearer planted"}}}}),
        )
    assert hook.resolve_connection(str(repo), env={"AGENT_CORE_API_KEY": "real"}, home=str(home)) == (None, None)
    _config(home, {"url": "https://core.example/mcp"})
    # the user's own URL pairs with the environment key; the workspace changes nothing
    assert hook.resolve_connection(str(repo), env={"AGENT_CORE_API_KEY": "real"}, home=str(home)) == ("https://core.example", "real")


def test_only_http_and_https_urls_are_used_and_bad_configs_are_skipped(tmp_path):
    import os
    import time

    hook = _hook()
    _config(tmp_path, {"url": "file:///etc/passwd", "headers": {"Authorization": "Bearer k"}})
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    for text in ("not json {", "[]", '{"mcpServers": {"agent-core": "nope"}}', '{"mcpServers": {"agent-core": {"url": 5, "headers": "x"}}}'):
        path.write_text(text)
        assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    started = time.time()
    path.unlink()
    path.symlink_to("/dev/zero")
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    path.unlink()
    os.mkfifo(path)
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    path.unlink()
    path.write_text('{"x": "' + "x" * (300 * 1024) + '"}')
    assert hook.resolve_connection("/x", env={}, home=str(tmp_path)) == (None, None)
    assert time.time() - started < 5


# --- SessionStart -------------------------------------------------------------------------------------------------


def test_session_start_asks_core_and_injects_the_context_as_a_system_message(monkeypatch):
    hook = _hook()
    sent = []

    def fake_post(base_url, api_key, path, payload):
        assert (base_url, api_key, path) == ("http://core", "k", "/api/session-context")
        sent.append(payload)
        return {"context": "# Agent Core session context: workspace:w"}

    monkeypatch.setattr(hook, "post", fake_post)
    event = {"hookEventName": "SessionStart", "conversationId": "c-1", "cwd": "/repo", "workspacePaths": ["/repo"], "modelName": "auto"}
    result = json.loads(_run(hook, event))
    assert result == {"injectSteps": [{"systemMessage": {"systemMessage": "# Agent Core session context: workspace:w"}}]}
    assert sent == [{"cwd": "/repo", "host_session_ref": "c-1", "source": "startup", "client_os": platform.system(), "max_chars": 9000}]
    # the working folder falls back to the first workspace path; a pinned workspace and a smaller budget are passed on
    sent.clear()
    _run(hook, {"hookEventName": "SessionStart", "conversationId": "c-2", "workspacePaths": ["/other"]},
         {**ENV, "AGENT_CORE_SCOPE": "workspace:w", "AGENT_CORE_CONTEXT_CHARS": "4000"})
    assert sent == [{"cwd": "/other", "host_session_ref": "c-2", "source": "startup", "client_os": platform.system(), "max_chars": 4000, "memory_scope": "workspace:w"}]
    # an event with no name is a start, as in the other hooks
    sent.clear()
    assert _run(hook, {"conversationId": "c-3", "cwd": "/r"})
    assert sent[0]["host_session_ref"] == "c-3"


def test_only_a_string_context_is_passed_on_and_it_is_capped(monkeypatch):
    hook = _hook()
    event = {"hookEventName": "SessionStart", "conversationId": "c", "cwd": "/repo"}
    for bad in (["x"], 5, {"a": 1}, ""):
        monkeypatch.setattr(hook, "post", lambda *a, _bad=bad: {"context": _bad})
        assert _run(hook, event) == ""
    monkeypatch.setattr(hook, "post", lambda *a: {"context": "x" * 50_000})
    text = json.loads(_run(hook, event))["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert len(text) == hook.MAX_CONTEXT_CHARS


def test_a_notice_for_the_person_carries_nothing_for_the_model(monkeypatch, tmp_path):
    hook = _hook()
    monkeypatch.setenv("HOME", str(tmp_path))  # never read the real ~/.gemini/config/mcp_config.json

    def failing(*_):
        raise OSError("connection refused")

    monkeypatch.setattr(hook, "post", failing)
    event = {"hookEventName": "SessionStart", "conversationId": "c", "cwd": "/repo"}
    raw = _run(hook, event, {"AGENT_CORE_URL": "http://c", "AGENT_CORE_API_KEY": "secret-key"})
    step = json.loads(raw)["injectSteps"][0]["systemMessage"]
    assert step["systemMessage"] == "" and "unavailable" in step["metadata"]["user_markdown"]
    assert "secret-key" not in raw and "\n" not in step["metadata"]["user_markdown"]
    # a key with a line break makes the request fail to build; the notice must not quote it
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    raw = _run(hook, event, {"AGENT_CORE_URL": "http://127.0.0.1:9", "AGENT_CORE_API_KEY": "abc\r\nX-Evil: 1"})
    assert "abc" not in raw and "X-Evil" not in raw and "unavailable" in raw
    # not configured at all, in a bare folder: silent
    assert _run(hook, event, {}) == ""


def test_no_connection_and_an_unmapped_folder_are_said_only_in_a_project_folder(monkeypatch, tmp_path):
    hook = _hook()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "home" / "work" / "repo"
    (project / ".git").mkdir(parents=True)
    event = {"hookEventName": "SessionStart", "conversationId": "c", "cwd": str(project)}
    note = json.loads(_run(hook, event, {}))["injectSteps"][0]["systemMessage"]
    assert note["systemMessage"] == "" and "no connection found" in note["metadata"]["user_markdown"]
    assert _run(hook, event, {"AGENT_CORE_NOTICES": "0"}) == ""
    monkeypatch.setattr(hook, "post", lambda *a: {"context": "", "resolved_by": "unmatched"})
    note = json.loads(_run(hook, event))["injectSteps"][0]["systemMessage"]
    assert "no workspace this agent can read is mapped" in note["metadata"]["user_markdown"]
    assert _run(hook, {**event, "cwd": str(tmp_path)}) == ""  # not a project folder


# --- Stop ---------------------------------------------------------------------------------------------------------


def test_stop_asks_once_for_a_handoff_when_the_turn_finished(monkeypatch):
    hook = _hook()
    calls = []

    def fake_post(base_url, api_key, path, payload):
        calls.append((path, payload))
        return {"nudge": True, "reason": "Record a handoff for this task."}

    monkeypatch.setattr(hook, "post", fake_post)
    event = {"hookEventName": "Stop", "conversationId": "c-1", "terminationReason": "NO_TOOL_CALL", "continuationActive": False, "fullyIdle": True}
    assert json.loads(_run(hook, event)) == {"decision": "continue", "reason": "Record a handoff for this task."}
    assert calls == [("/api/session-context/handoff-check", {"host_session_ref": "c-1"})]
    # a reason of "model_stop" (the web docs' name) and no reason at all are the same ending
    for reason in ("model_stop", None):
        e = {k: v for k, v in event.items() if k != "terminationReason"}
        assert _run(hook, {**e, **({"terminationReason": reason} if reason else {})})


def test_stop_stays_silent_when_it_should_not_keep_the_agent_going(monkeypatch):
    hook = _hook()
    calls = []
    monkeypatch.setattr(hook, "post", lambda *a: calls.append(a) or {"nudge": True, "reason": "r"})
    base = {"hookEventName": "Stop", "conversationId": "c", "terminationReason": "NO_TOOL_CALL"}
    for event in (
        {**base, "continuationActive": True},  # already continuing because of a stop hook: never loop
        {**base, "fullyIdle": False},  # background work is still running
        {**base, "terminationReason": "USER_CANCELED"},
        {**base, "terminationReason": "ERROR"},
        {**base, "terminationReason": "MAX_INVOCATIONS"},
        {**base, "terminationReason": "MAX_FORCED_INVOCATIONS"},
        {**base, "terminationReason": "TERMINAL_CUSTOM_HOOK"},
        {k: v for k, v in base.items() if k != "conversationId"},
    ):
        assert _run(hook, event) == "", event
    assert _run(hook, base, {**ENV, "AGENT_CORE_HANDOFF_NUDGE": "0"}) == ""
    assert calls == []


def test_stop_never_fails_because_core_is_unreachable_or_says_nothing(monkeypatch):
    hook = _hook()
    event = {"hookEventName": "Stop", "conversationId": "c", "terminationReason": "NO_TOOL_CALL"}

    def failing(*_):
        raise OSError("down")

    monkeypatch.setattr(hook, "post", failing)
    assert _run(hook, event) == ""
    for answer in ({"nudge": False}, {"nudge": True}, {"nudge": True, "reason": 5}, {}):
        monkeypatch.setattr(hook, "post", lambda *a, _a=answer: _a)
        assert _run(hook, event) == ""


def test_malformed_events_exit_cleanly():
    hook = _hook()
    for stdin in ("[]", '"text"', "{not json", "", json.dumps({"hookEventName": "PreInvocation"}), json.dumps({"hookEventName": "Other"})):
        out = io.StringIO()
        assert hook.main(stdin=io.StringIO(stdin), stdout=out, env=ENV) == 0
        assert out.getvalue() == ""


# --- the same requests as the other hooks, never leaking a key ----------------------------------------------------


def test_it_sends_the_same_request_as_the_codex_and_claude_hooks(monkeypatch):
    antigravity, codex, claude = _hook(), _load("codex", "agent_core_hook.py"), _load("claude-code", "agent_core_hook.py")
    env = {**ENV, "AGENT_CORE_SCOPE": "workspace:w"}
    seen = []
    for module, event in (
        (antigravity, {"hookEventName": "SessionStart", "conversationId": "s1", "cwd": "/repo"}),
        (codex, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/repo", "source": "startup"}),
        (claude, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/repo", "source": "startup"}),
    ):
        monkeypatch.setattr(module, "post", lambda base, key, path, payload: seen.append((path, payload)) or {"context": "ctx"})
        module.main(io.StringIO(json.dumps(event)), io.StringIO(), env)
    assert seen[0] == seen[1] == seen[2]


def test_a_redirect_is_never_followed_so_the_key_stays_with_its_own_server():
    import http.server
    import threading

    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
            self.end_headers()

        def log_message(self, *_):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hook = _hook()
        try:
            hook.post("http://127.0.0.1:%d" % redirector.server_port, "SECRET-KEY", "/api/session-context", {})
        except Exception as exc:
            assert "SECRET-KEY" not in str(exc)
        else:
            raise AssertionError("a redirect was followed")
        env = {"AGENT_CORE_URL": f"http://127.0.0.1:{redirector.server_port}", "AGENT_CORE_API_KEY": "SECRET-KEY"}
        raw = _run(hook, {"hookEventName": "SessionStart", "conversationId": "s", "cwd": "/r"}, env)
        assert "SECRET-KEY" not in raw and "unavailable" in raw
        assert seen == []
    finally:
        target.shutdown()
        redirector.shutdown()


# --- against the real server --------------------------------------------------------------------------------------


def test_a_session_start_and_a_stop_against_the_real_server(test_client, agent_token, monkeypatch):
    from app.database import get_db
    from app.services import activity_service

    hook = _hook()

    def through_the_app(base_url, api_key, path, payload):
        assert api_key == agent_token
        response = test_client.post(path, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        assert response.status_code == 200, response.json()
        return response.json()["data"]

    monkeypatch.setattr(hook, "post", through_the_app)
    env = {"AGENT_CORE_URL": "http://core", "AGENT_CORE_API_KEY": agent_token, "AGENT_CORE_SCOPE": "agent:testagent"}
    started = json.loads(_run(hook, {"hookEventName": "SessionStart", "conversationId": "agy-1", "cwd": "/repo"}, env))
    context = started["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert context.startswith("# Agent Core session context: agent:testagent")

    with get_db() as conn:
        row = conn.execute("SELECT id FROM agent_executions WHERE host_session_ref = 'agy-1'").fetchone()
        owner = conn.execute("SELECT user_id FROM agent_executions WHERE host_session_ref = 'agy-1'").fetchone()["user_id"]
    assert row, "the session start was recorded under the conversation id"
    activity_service.create_activity(
        agent_id="testagent", user_id=owner, task_description="Left open", memory_scope="agent:testagent",
        source_execution_id=row["id"],
    )
    stop = {"hookEventName": "Stop", "conversationId": "agy-1", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True}
    first = json.loads(_run(hook, stop, env))
    assert first["decision"] == "continue" and first["reason"]
    assert _run(hook, stop, env) == "", "Core asks at most once per task"


# --- what the Connect page generates ------------------------------------------------------------------------------


def test_the_script_is_served_and_the_generated_steps_register_it_in_hooks_json(test_client):
    from app.routes.integrations_page import KNOWN_OUTPUT_TYPES, _build_antigravity_hook

    response = test_client.get("/integrations/hooks/antigravity/agent_core_hook.py")
    assert response.status_code == 200 and "def resolve_connection" in response.text
    text = _build_antigravity_hook("https://core.example")
    assert "antigravity_hook" in KNOWN_OUTPUT_TYPES
    assert "https://core.example/integrations/hooks/antigravity/agent_core_hook.py" in text
    assert "~/.gemini/config/hooks.json" in text and ".agents/hooks.json" in text and "settings.json" in text
    assert "Bearer" not in text and "sk-" not in text
    registered = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    group = registered["agent-core"]
    start, stop = group["SessionStart"][0], group["Stop"][0]
    assert start == {"type": "command", "command": "python3 ~/.gemini/agent-core/agent_core_hook.py", "timeout": 15}
    assert stop == {"type": "command", "command": start["command"], "timeout": 10}
    assert "matcher" not in start and "hooks" not in group  # flat handler lists, as Antigravity requires
    # the Windows steps use the full path with forward slashes, in the folder Antigravity reads
    assert 'python \\"C:/Users/YOU/.gemini/agent-core/agent_core_hook.py\\"' in text
    assert "$env:USERPROFILE\\.gemini\\config\\hooks.json" in text
    # the try-by-hand command speaks Antigravity's camelCase
    assert '"hookEventName":"SessionStart","conversationId":"try-1"' in text and "hook_event_name" not in text
    # the same warnings as the other hooks, plus the rule that a delivered handoff is not an approval
    assert "Can Read From" in text and "behaves exactly like an unmapped one" in text
    assert "never a person's approval" in text


def test_the_antigravity_preset_writes_the_current_mcp_fields():
    from app.routes.integrations_page import _build_mcp_json

    block = _build_mcp_json("https://core.test", "K", tool="antigravity")
    assert '"url": "https://core.test/mcp"' in block and '"Authorization": "Bearer K"' in block
    assert "serverUrl" not in block and '"type"' not in block
