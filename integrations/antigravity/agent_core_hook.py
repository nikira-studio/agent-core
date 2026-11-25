#!/usr/bin/env python3
"""Antigravity hook for Agent Core: session context in, handoff out.

Register it for two events (see the Antigravity Hook output on the Integrations page);
it reads hookEventName from the event on stdin.

SessionStart: asks Agent Core for one budgeted block (standing rules, work waiting
on a person, unfinished and handed-off work, recent results, changes since the
agent's last session, an index of key records) and hands it to Antigravity as a
system message, so the agent does not have to remember a startup routine.

Stop: when the session has an open activity without a structured handoff, it keeps
the agent going once with a request to record one. Agent Core remembers that it
asked, so a task costs at most one extra turn, however many replies end.

It is the sibling of integrations/codex/agent_core_hook.py and integrations/claude-code/
agent_core_hook.py and sends the same requests to the same endpoints
(/api/session-context and /api/session-context/handoff-check); what the context says
and when to nudge are decided by Agent Core, not here. A handoff delivered this way is
information for the agent, never a person's approval of anything.

Antigravity sends camelCase JSON on stdin (hookEventName, conversationId, cwd,
workspacePaths) and reads JSON on stdout: {"injectSteps": [{"systemMessage":
{"systemMessage": "..."}}]} to add context, {"decision": "continue", "reason": "..."}
from Stop to keep the agent going. A notice meant only for the person goes in
systemMessage.metadata.user_markdown with an empty systemMessage, so the model sees nothing.

Configuration, first match wins. Only the user's own files and environment are
read, never a repository's, so a cloned project can neither redirect the key nor
name an environment variable to send:
  AGENT_CORE_URL and AGENT_CORE_API_KEY  environment variables, both set
  the Agent Core MCP server in ~/.gemini/config/mcp_config.json: its url (or
  serverUrl) and its Authorization header, so a rotated key is picked up automatically.
  AGENT_CORE_API_KEY alone pairs with the URL from that file.
  AGENT_CORE_MCP_NAME  MCP server name to look for (default: agent-core)
  AGENT_CORE_SCOPE     workspace scope; otherwise the server matches the cwd
                       against its configured workspace repo roots
  AGENT_CORE_CONTEXT_CHARS  session-start budget (default 9000)
  AGENT_CORE_HANDOFF_NUDGE=0  turn the Stop nudge off
  AGENT_CORE_NOTICES=0  turn off the one-line notices about a folder with no
                       workspace and a project with no Agent Core connection

It never breaks a session: any failure exits 0, at most with a one-line notice
that never carries the key, a redirect is never followed (the key would go with
it), and the whole run is cut off after HARD_DEADLINE_SECONDS.
Standard library only; Python 3.8 or newer.
"""

import json
import os
import platform
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 8
HARD_DEADLINE_SECONDS = 12  # under the 15s the install text registers; covers DNS and slow servers
MAX_CONFIG_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 10_000
DEFAULT_CHARS = 9000
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, urllib.error.URLError)
# A turn that ended because the model finished. Antigravity names it NO_TOOL_CALL; its web docs also show
# "model_stop". Every other reason (cancelled, error, limits, a hook's own stop) is left alone.
FINISHED_REASONS = {"NO_TOOL_CALL", "model_stop"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _bearer(value):
    if isinstance(value, str) and value[:7].lower() == "bearer ":
        return value[7:].strip()
    return ""


def _user_config(home):
    return os.path.join(home, ".gemini", "config", "mcp_config.json")


def _mcp_server(config_path, name):
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
    server = _dict(config.get("mcpServers")).get(name)
    return server if isinstance(server, dict) else None


def _server_key(server):
    """The bearer token an Antigravity MCP server entry carries in its headers."""
    for header, value in _dict(server.get("headers")).items():
        if isinstance(header, str) and header.lower() == "authorization" and _bearer(value):
            return _bearer(value)
    return ""


def no_connection_notice(env, home=None):
    """One line for a project that has the hook but no Agent Core connection: which file was read, never a key."""
    path = _user_config(home or os.path.expanduser("~"))
    return (
        f"Agent Core: no connection found, so this session starts without shared context. Looked in {path} for an "
        f"MCP server named {env.get('AGENT_CORE_MCP_NAME', 'agent-core')}; add it there (Connect page, step a)."
    )


def resolve_connection(cwd, env=os.environ, home=None):
    """(base_url, api_key) or (None, None). cwd is accepted for symmetry with the other hooks and unused:
    a workspace's own .agents/mcp_config.json is deliberately never read."""
    url = env.get("AGENT_CORE_URL", "").rstrip("/")
    key = env.get("AGENT_CORE_API_KEY", "")
    if url and key:
        return url, key
    name = env.get("AGENT_CORE_MCP_NAME", "agent-core")
    server = _mcp_server(_user_config(home or os.path.expanduser("~")), name)
    if not server:
        return None, None
    server_url = server.get("url") if isinstance(server.get("url"), str) else server.get("serverUrl")
    if not isinstance(server_url, str):
        return None, None
    server_url = server_url.rstrip("/")
    if server_url.endswith("/mcp"):
        server_url = server_url[: -len("/mcp")]
    server_key = _server_key(server) or key
    if urllib.parse.urlsplit(server_url).scheme not in ("http", "https") or not server_key:
        return None, None
    return server_url, server_key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the key travels in a header: a redirect would carry it to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


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


def _context_step(text):
    """What the model reads."""
    return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}


def _notice_step(text):
    """A line for the person only: the model is sent an empty message."""
    return {"injectSteps": [{"systemMessage": {"systemMessage": "", "metadata": {"user_markdown": text}}}]}


PROJECT_MARKERS = (".git", "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".agents")
UNMAPPED_NOTICE = (
    "Agent Core: no workspace this agent can read is mapped to this folder, so this session starts "
    "without shared context. Map the folder in Workspaces > Edit Workspace > Repository folders, "
    "and check the agent's access on the Agents page."
)


def _looks_like_project(cwd, home=None):
    """True when cwd or a parent below the home directory holds a project marker.

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


def _working_folder(event):
    paths = event.get("workspacePaths")
    return event.get("cwd") or (paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], str) else None) or os.getcwd()


def session_start(event, base_url, api_key, env):
    try:
        chars = int(env.get("AGENT_CORE_CONTEXT_CHARS", DEFAULT_CHARS))
    except ValueError:
        chars = DEFAULT_CHARS
    cwd = _working_folder(event)
    payload = {
        "cwd": cwd,
        "host_session_ref": event.get("conversationId"),
        "source": "startup",
        "client_os": platform.system()[:40],
        "max_chars": max(1000, min(chars, 9500)),
    }
    if env.get("AGENT_CORE_SCOPE"):
        payload["memory_scope"] = env["AGENT_CORE_SCOPE"]
    try:
        data = post(base_url, api_key, "/api/session-context", payload)
    except NETWORK_ERRORS as exc:
        return _notice_step(f"Agent Core session context unavailable: {_reason(exc, api_key)}")
    context = data.get("context")
    if not isinstance(context, str) or not context:
        if data.get("resolved_by") == "unmatched" and env.get("AGENT_CORE_NOTICES") != "0" and _looks_like_project(cwd):
            return _notice_step(UNMAPPED_NOTICE)
        return None
    return _context_step(context[:MAX_CONTEXT_CHARS])


def stop(event, base_url, api_key, env):
    if event.get("continuationActive") or env.get("AGENT_CORE_HANDOFF_NUDGE") == "0":
        return None  # already continuing because of a stop hook: never loop
    if not event.get("conversationId"):
        return None
    if event.get("fullyIdle") is False:
        return None  # background work is still running: the turn has not really finished
    reason = event.get("terminationReason")
    if reason and reason not in FINISHED_REASONS:
        return None  # cancelled, failed or cut off: nothing to ask the agent to record
    try:
        data = post(
            base_url, api_key, "/api/session-context/handoff-check",
            {"host_session_ref": event["conversationId"]},
        )
    except NETWORK_ERRORS:
        return None  # a stop must never fail because Agent Core is unreachable
    if not data.get("nudge") or not isinstance(data.get("reason"), str):
        return None
    return {"decision": "continue", "reason": data["reason"]}


HANDLERS = {"SessionStart": session_start, "Stop": stop}


def main(stdin=sys.stdin, stdout=sys.stdout, env=os.environ):
    try:
        return _run(stdin, stdout, env)
    except Exception:  # never fail the session, whatever went wrong
        return 0


def _hard_deadline():
    timer = threading.Timer(HARD_DEADLINE_SECONDS, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    return timer


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
    handler = HANDLERS.get(event.get("hookEventName", "SessionStart"))
    if handler is None:
        return 0
    cwd = _working_folder(event)
    base_url, bearer = resolve_connection(cwd, env)
    if not base_url:
        # Silent unless a session starts in a project folder: there the person expects shared context.
        if (
            handler is session_start
            and env.get("AGENT_CORE_NOTICES") != "0"
            and _looks_like_project(cwd)
        ):
            json.dump(_notice_step(no_connection_notice(env)), stdout)
        return 0
    output = handler(event, base_url, bearer, env)
    if output:
        json.dump(output, stdout)
    return 0


if __name__ == "__main__":
    _hard_deadline()
    sys.exit(main())
