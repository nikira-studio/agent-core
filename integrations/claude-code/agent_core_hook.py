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

Configuration, first match wins:
  AGENT_CORE_URL / AGENT_CORE_API_KEY  environment variables
  the Agent Core MCP server in .mcp.json (project) or ~/.claude.json (user):
  its URL and Authorization header, so a rotated key is picked up automatically.
  AGENT_CORE_MCP_NAME  MCP server name to look for (default: agent-core)
  AGENT_CORE_SCOPE     workspace scope; otherwise the server matches the cwd
                       against its configured workspace repo roots
  AGENT_CORE_CONTEXT_CHARS  session-start budget (default 9000; Claude Code
                       drops anything over 10,000 characters to a stub)
  AGENT_CORE_HANDOFF_NUDGE=0  turn the Stop nudge off

It never breaks a session: any failure exits 0, at most with a one-line notice.
Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request

INJECT_SOURCES = {"startup", "clear", "compact"}
TIMEOUT_SECONDS = 8
DEFAULT_CHARS = 9000
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, urllib.error.URLError)


def _mcp_server(config_path, name, project=None):
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return None
    servers = config.get("mcpServers") or {}
    if project:
        # ~/.claude.json also keeps per-project servers under projects.<path>.
        project_servers = ((config.get("projects") or {}).get(project) or {}).get("mcpServers") or {}
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
    candidates = [
        (os.path.join(project_dir, ".mcp.json"), None),
        (os.path.join(home, ".claude.json"), project_dir),
    ]
    for path, project in candidates:
        server = _mcp_server(path, name, project)
        if not server or not server.get("url"):
            continue
        header = (server.get("headers") or {}).get("Authorization", "")
        server_key = header[7:] if header.startswith("Bearer ") else ""
        server_url = server["url"].rstrip("/")
        if server_url.endswith("/mcp"):
            server_url = server_url[: -len("/mcp")]
        if (url or server_url) and (key or server_key):
            return url or server_url, key or server_key
    return None, None


def post(base_url, api_key, path, payload):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError((body.get("error") or {}).get("message", "request failed"))
    return body.get("data") or {}


def session_start(event, base_url, api_key, env):
    if event.get("source", "startup") not in INJECT_SOURCES:
        return None  # resume and fork carry their context already
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
        return {"systemMessage": f"Agent Core session context unavailable: {exc}"}
    if not data.get("context"):
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": data["context"]}}


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
        event = json.load(stdin)
    except ValueError:
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


if __name__ == "__main__":
    sys.exit(main())
