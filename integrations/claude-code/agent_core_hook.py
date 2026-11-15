#!/usr/bin/env python3
"""Claude Code hook for Agent Core: session context in, handoff out.

Register it for two events; it reads hook_event_name from the event on stdin.

SessionStart: asks Agent Core for one budgeted block (standing rules, unfinished
and handed-off work, recent results, changes since the agent's last session, an
index of key records) and hands it to Claude Code as additionalContext, so the
agent does not have to remember a startup ritual.

Stop: when the session has an open activity without a structured handoff, it
blocks the stop once with a request to record one. Agent Core remembers that it
asked, so a task costs at most one extra turn, however many replies end.

Configuration, first match wins; the URL and the key always come from the
same place, so a repository's .mcp.json can never redirect a key it did not
supply:
  AGENT_CORE_URL and AGENT_CORE_API_KEY  environment variables, both set
  the Agent Core MCP server in .mcp.json (project) or ~/.claude.json (user; in
  $CLAUDE_CONFIG_DIR/.claude.json when that is set):
  its URL and Authorization header, so a rotated key is picked up automatically.
  AGENT_CORE_API_KEY alone pairs only with a URL from ~/.claude.json, which the
  user controls, never with one from a project's .mcp.json.
  AGENT_CORE_MCP_NAME  MCP server name to look for (default: agent-core)
  AGENT_CORE_SCOPE     workspace scope; otherwise the server matches the cwd
                       against its configured workspace repo roots
  AGENT_CORE_CONTEXT_CHARS  session-start budget (default 9000; Claude Code
                       drops anything over 10,000 characters to a stub)
  AGENT_CORE_HANDOFF_NUDGE=0  turn the Stop nudge off

It never breaks a session: any failure exits 0, at most with a one-line notice
that never carries the key; a redirect is never followed (the key would go with
it) and the run is cut off after HARD_DEADLINE_SECONDS. Standard library only.
"""

import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request

# resume is asked about too: Agent Core answers only for a session that never received context for this
# workspace (the folder was unmapped when it started), and a session that has it hears nothing. A fork carries its parent's.
INJECT_SOURCES = {"startup", "clear", "compact", "resume"}
TIMEOUT_SECONDS = 8
HARD_DEADLINE_SECONDS = 12  # under the 15s the install text registers; covers DNS and slow servers
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 10_000
DEFAULT_CHARS = 9000
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, urllib.error.URLError)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _mcp_server(config_path, name, project=None):
    try:
        # A regular file of sane size only: a symlink to /dev/zero or a FIFO must not hang a session.
        if not stat.S_ISREG(os.stat(config_path).st_mode):
            return None
        with open(config_path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        config = _dict(json.loads(raw.decode("utf-8")))
    except Exception:  # unreadable, not UTF-8, not JSON, nested too deep: treat as absent
        return None
    servers = _dict(config.get("mcpServers"))
    if project:
        # ~/.claude.json also keeps per-project servers under projects.<path>.
        project_servers = _dict(_dict(_dict(config.get("projects")).get(project)).get("mcpServers"))
        servers = {**servers, **project_servers}
    server = servers.get(name)
    return server if isinstance(server, dict) else None


def resolve_connection(cwd, env=os.environ, home=None):
    """(base_url, api_key) or (None, None)."""
    url = env.get("AGENT_CORE_URL", "").rstrip("/")
    key = env.get("AGENT_CORE_API_KEY", "")
    if url and key:
        return url, key
    name = env.get("AGENT_CORE_MCP_NAME", "agent-core")
    home = home or os.path.expanduser("~")
    project_dir = env.get("CLAUDE_PROJECT_DIR") or cwd
    # Claude Code keeps its user configuration in ~/.claude.json, or in .claude.json inside CLAUDE_CONFIG_DIR when set.
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    user_config = os.path.join(config_dir, ".claude.json") if config_dir else os.path.join(home, ".claude.json")
    # (config file, project key, whether its URL may carry the env key)
    candidates = [
        (os.path.join(project_dir, ".mcp.json"), None, False),
        (user_config, project_dir, True),
    ]
    for path, project, trusted in candidates:
        server = _mcp_server(path, name, project)
        if not server or not isinstance(server.get("url"), str):
            continue
        header = _dict(server.get("headers")).get("Authorization", "")
        server_key = header[7:].strip() if isinstance(header, str) and header[:7].lower() == "bearer " else ""
        server_url = server["url"].rstrip("/")
        if server_url.endswith("/mcp"):
            server_url = server_url[: -len("/mcp")]
        if server_url and server_key:
            return server_url, server_key
        if server_url and key and trusted:
            return server_url, key
    return None, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the key travels in a header: a redirect would carry it to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def _reason(exc, api_key):
    """One short line about a failure, never the key and never raw exception text that could hold it."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "cannot reach it"
    if isinstance(exc, RuntimeError):  # Agent Core's own error message
        text = "".join(ch for ch in str(exc) if ch.isprintable())[:120]
        return text.replace(api_key, "***") if api_key else text
    return type(exc).__name__


def post(base_url, api_key, path, payload):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
        body = json.loads(response.read(MAX_RESPONSE_BYTES).decode("utf-8"))
    if not isinstance(body, dict) or not body.get("ok"):
        error = body.get("error") if isinstance(body, dict) else None
        raise RuntimeError(_dict(error).get("message") or str(error or "request failed"))
    return _dict(body.get("data"))


PROJECT_MARKERS = (".git", "AGENTS.md", "CLAUDE.md")
UNMAPPED_NOTICE = (
    "Agent Core: no workspace this agent can read is mapped to this folder, so this session starts "
    "without shared context. Map the folder in Workspaces > Edit Workspace > Repository folder, "
    "and check the agent's access on the Agents page."
)


def _looks_like_project(cwd, home=None):
    """True when cwd or a parent below the home directory holds .git, AGENTS.md or CLAUDE.md.

    The home directory itself, the filesystem root and bare folders such as /tmp do not count, so the
    notice about an unmapped folder never appears where nobody expects a workspace."""
    try:
        home = os.path.realpath(home or os.path.expanduser("~"))
        directory = os.path.realpath(cwd)
        while directory not in (home, os.sep) and os.path.dirname(directory) != directory:
            if any(os.path.exists(os.path.join(directory, marker)) for marker in PROJECT_MARKERS):
                return True
            directory = os.path.dirname(directory)
    except (OSError, ValueError, TypeError):
        pass
    return False


def session_start(event, base_url, api_key, env):
    if event.get("source", "startup") not in INJECT_SOURCES:
        return None  # a fork carries its parent's context
    try:
        chars = int(env.get("AGENT_CORE_CONTEXT_CHARS", DEFAULT_CHARS))
    except ValueError:
        chars = DEFAULT_CHARS
    payload = {
        "cwd": event.get("cwd") or os.getcwd(),
        "host_session_ref": event.get("session_id"),
        "source": event.get("source", "startup"),
        "max_chars": max(1000, min(chars, 9500)),
    }
    if env.get("AGENT_CORE_SCOPE"):
        payload["memory_scope"] = env["AGENT_CORE_SCOPE"]
    try:
        data = post(base_url, api_key, "/api/session-context", payload)
    except NETWORK_ERRORS as exc:
        return {"systemMessage": f"Agent Core session context unavailable: {_reason(exc, api_key)}"}
    context = data.get("context")
    if not isinstance(context, str) or not context:
        if data.get("resolved_by") == "unmatched" and _looks_like_project(payload["cwd"]):
            return {"systemMessage": UNMAPPED_NOTICE}
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context[:MAX_CONTEXT_CHARS]}}


def stop(event, base_url, api_key, env):
    if event.get("stop_hook_active") or env.get("AGENT_CORE_HANDOFF_NUDGE") == "0":
        return None  # already continuing because of a stop hook: never loop
    if not event.get("session_id"):
        return None
    try:
        data = post(
            base_url, api_key, "/api/session-context/handoff-check",
            {"host_session_ref": event["session_id"]},
        )
    except NETWORK_ERRORS:
        return None  # a stop must never fail because Agent Core is unreachable
    if not data.get("nudge"):
        return None
    return {"decision": "block", "reason": data["reason"]}


HANDLERS = {"SessionStart": session_start, "Stop": stop}


def main(stdin=sys.stdin, stdout=sys.stdout, env=os.environ):
    try:
        return _run(stdin, stdout, env)
    except Exception:  # never fail the session, whatever went wrong
        return 0


def _run(stdin, stdout, env):
    try:
        # Read bytes and decode as UTF-8: on Windows the default text decoding is the
        # system code page, which would garble a folder name with accents.
        raw = stdin.buffer.read().decode("utf-8", "replace") if hasattr(stdin, "buffer") else stdin.read()
        event = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    handler = HANDLERS.get(event.get("hook_event_name", "SessionStart"))
    if handler is None:
        return 0
    base_url, api_key = resolve_connection(event.get("cwd") or os.getcwd(), env)
    if not base_url:
        return 0  # not configured for this project: stay silent
    output = handler(event, base_url, api_key, env)
    if output:
        json.dump(output, stdout)
    return 0


def _hard_deadline():
    timer = threading.Timer(HARD_DEADLINE_SECONDS, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    return timer


if __name__ == "__main__":
    _hard_deadline()
    sys.exit(main())
