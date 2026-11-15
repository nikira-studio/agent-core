#!/usr/bin/env python3
"""Codex hook for Agent Core: session context in.

Register it for the SessionStart event (see the Codex Hook output on the
Integrations page). It asks Agent Core for one budgeted block (standing rules,
work waiting on a person, unfinished and handed-off work, recent results, changes
since the agent's last session, an index of key records) and hands it to Codex as
additionalContext, so the agent does not have to remember a startup routine.

It is the sibling of integrations/claude-code/agent_core_hook.py and sends the
same request to the same endpoint (/api/session-context); the context itself is
built by Agent Core, so nothing here decides what an agent sees. It has no Stop
handler: Codex does not need the Claude Code handoff nudge to use this.

Configuration, first match wins. Only the user's own files and environment are
read, never a repository's, so a cloned project can neither redirect the key nor
name an environment variable to send:
  AGENT_CORE_URL and AGENT_CORE_API_KEY  environment variables, both set
  the Agent Core MCP server in ~/.codex/config.toml (or $CODEX_HOME/config.toml):
  its URL and its Authorization header, bearer_token_env_var or env_http_headers
  entry, so a rotated key is picked up automatically.
  AGENT_CORE_API_KEY alone pairs with the URL from that config.toml.
  AGENT_CORE_MCP_NAME  MCP server name to look for (default: agent-core)
  AGENT_CORE_SCOPE     workspace scope; otherwise the server matches the cwd
                       against its configured workspace repo roots
  AGENT_CORE_CONTEXT_CHARS  session-start budget (default 9000)
  AGENT_CORE_NOTICES=0  turn off the one-line notices about a folder with no
                       workspace and a project with no Agent Core connection

It never breaks a session: any failure exits 0, at most with a one-line notice
that never carries the key, a redirect is never followed (the key would go with
it), and the whole run is cut off after HARD_DEADLINE_SECONDS.
Needs python3 3.11 or newer (tomllib); standard library only.
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

try:
    import tomllib
except ImportError:  # python older than 3.11: stay silent rather than fail a session
    tomllib = None

# resume is asked about too: Agent Core answers only for a session that never received context for this
# workspace (the folder was unmapped when it started), and a session that has it hears nothing. A fork carries its parent's.
INJECT_SOURCES = {"startup", "clear", "compact", "resume"}
TIMEOUT_SECONDS = 8
HARD_DEADLINE_SECONDS = 12  # under the 15s the install text registers; covers DNS and slow servers
MAX_CONFIG_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 10_000
DEFAULT_CHARS = 9000
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, urllib.error.URLError)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _bearer(value):
    if isinstance(value, str) and value[:7].lower() == "bearer ":
        return value[7:].strip()
    return ""


def _mcp_server(config_path, name):
    if tomllib is None:
        return None
    try:
        # A regular file of sane size only: a symlink to /dev/zero or a FIFO must not hang a session.
        if not stat.S_ISREG(os.stat(config_path).st_mode):
            return None
        with open(config_path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        config = _dict(tomllib.loads(raw.decode("utf-8")))
    except Exception:  # unreadable, not UTF-8, not TOML, nested too deep: treat as absent
        return None
    server = _dict(config.get("mcp_servers")).get(name)
    return server if isinstance(server, dict) else None


def _server_key(server, env):
    """The bearer token a Codex MCP server entry carries, however it is declared."""
    for header, value in _dict(server.get("http_headers")).items():
        if isinstance(header, str) and header.lower() == "authorization" and _bearer(value):
            return _bearer(value)
    var = server.get("bearer_token_env_var")
    if isinstance(var, str) and env.get(var):
        return env[var].strip()
    for header, var in _dict(server.get("env_http_headers")).items():
        if isinstance(header, str) and header.lower() == "authorization" and isinstance(var, str):
            return _bearer(env.get(var, ""))
    return ""


def _user_config(env, home):
    return os.path.join(env.get("CODEX_HOME") or os.path.join(home, ".codex"), "config.toml")


def no_connection_notice(env, home=None):
    """One line for a project that has the hook but no Agent Core connection: which file was read, never a key."""
    path = _user_config(env, home or os.path.expanduser("~"))
    return (
        f"Agent Core: no connection found, so this session starts without shared context. Looked in {path} for an "
        f"MCP server named {env.get('AGENT_CORE_MCP_NAME', 'agent-core')}; add it there (Connect page, step a)."
    )


def resolve_connection(cwd, env=os.environ, home=None):
    """(base_url, api_key) or (None, None). cwd is accepted for symmetry with the Claude hook and unused:
    a repository's own .codex/config.toml is deliberately never read."""
    url = env.get("AGENT_CORE_URL", "").rstrip("/")
    key = env.get("AGENT_CORE_API_KEY", "")
    if url and key:
        return url, key
    name = env.get("AGENT_CORE_MCP_NAME", "agent-core")
    server = _mcp_server(_user_config(env, home or os.path.expanduser("~")), name)
    if not server or not isinstance(server.get("url"), str):
        return None, None
    server_url = server["url"].rstrip("/")
    if server_url.endswith("/mcp"):
        server_url = server_url[: -len("/mcp")]
    server_key = _server_key(server, env) or key
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


PROJECT_MARKERS = (".git", "AGENTS.md", "CLAUDE.md")
UNMAPPED_NOTICE = (
    "Agent Core: no workspace this agent can read is mapped to this folder, so this session starts "
    "without shared context. Map the folder in Workspaces > Edit Workspace > Repository folders, "
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
        return None
    try:
        chars = int(env.get("AGENT_CORE_CONTEXT_CHARS", DEFAULT_CHARS))
    except ValueError:
        chars = DEFAULT_CHARS
    payload = {
        "cwd": event.get("cwd") or os.getcwd(),
        "host_session_ref": event.get("session_id"),
        "source": event.get("source", "startup"),
        "client_os": platform.system(),
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
        if data.get("resolved_by") == "unmatched" and env.get("AGENT_CORE_NOTICES") != "0" and _looks_like_project(payload["cwd"]):
            return {"systemMessage": UNMAPPED_NOTICE}
        return None
    data = {"context": context[:MAX_CONTEXT_CHARS]}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": data["context"]}}


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
    if not isinstance(event, dict) or event.get("hook_event_name", "SessionStart") != "SessionStart":
        return 0
    cwd = event.get("cwd") or os.getcwd()
    base_url, bearer = resolve_connection(cwd, env)
    if not base_url:
        # Silent unless a session starts in a project folder: there the person expects shared context.
        if (
            event.get("source", "startup") in INJECT_SOURCES
            and env.get("AGENT_CORE_NOTICES") != "0"
            and _looks_like_project(cwd)
        ):
            json.dump({"systemMessage": no_connection_notice(env)}, stdout)
        return 0
    output = session_start(event, base_url, bearer, env)
    if output:
        json.dump(output, stdout)
    return 0


if __name__ == "__main__":
    _hard_deadline()
    sys.exit(main())
