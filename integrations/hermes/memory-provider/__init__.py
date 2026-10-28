"""Agent Core memory provider — prefetch-only HTTP recall.

Recalls relevant records from an Agent Core instance's ``/api/memory/search``
endpoint before every turn and returns them as RAW context text. The Hermes
conversation loop wraps the merged provider output in a ``<memory-context>``
block exactly once (``build_memory_context_block`` in
``agent/conversation_loop.py`` — see ``prefetch_all`` -> ``build_memory_context_block``),
so this provider must return RAW text and must NOT pre-wrap with the fence.
(Bundled providers like ``retaindb`` do the same — they return raw text.)

Why this exists
---------------
A behavior that must happen on every turn ("recall before answering") cannot be
made reliable with a model-facing rule. ``prefetch()`` is the deterministic hook:
the recall lands in the model's context *before* it drafts a reply, so it cannot
"forget" to check.

Design
------
* Prefetch-only. ``get_tool_schemas() -> []`` — no tools added to the prompt.
  Explicit reads/writes already exist as Agent Core MCP tools; the entire point
  of this provider is automatic recall, not more tools.
* Nothing is captured per turn. ``sync_turn()`` is a no-op; the agent writes
  through the ordinary Agent Core tools (``memory_write``, ``activity_update``),
  and the system prompt block tells it to.
* Fails safe. Any error in ``prefetch()`` returns ``""`` and logs at debug — a
  missing or broken Agent Core never breaks the agent loop. ``is_available()``
  returns ``False`` when no bearer token can be found, so the loader skips the
  provider cleanly.

Where the URL + token come from (first match wins)
--------------------------------------------------
1. Environment: ``AGENT_CORE_URL`` and ``AGENT_CORE_API_KEY``
   (``AGENT_CORE_BEARER`` accepted as a token alias).
2. The Hermes config's existing Agent Core MCP server block —
   ``mcp_servers.agent_core`` in ``$HERMES_HOME/config.yaml`` (or
   ``~/.hermes/config.yaml``). The token is read from
   ``headers.Authorization`` (``Bearer `` stripped) and the REST base URL is
   derived from ``url`` (a trailing ``/mcp`` or ``/sse`` is removed).
   ``${env:NAME}`` and ``${NAME}`` in either are expanded from the environment,
   as Hermes does for the MCP server itself. If a referenced variable is unset,
   the provider logs one warning naming it and stays unavailable.

This means you do NOT have to duplicate the bearer: if Agent Core is already
wired as an MCP server, the provider reuses that same single source of truth.

Other config (environment variables)
------------------------------------
* ``AGENT_CORE_SCOPE``    Not used: recall uses the key's default scopes.
                          Agent Core applies the key's default recall scopes.
                          Default: unset
* ``AGENT_CORE_LIMIT``    Max records injected per turn. Default: ``5``
* ``AGENT_CORE_MAX_CONTEXT_CHARS``  Max characters injected per turn.
                          Default: ``12000``
* ``AGENT_CORE_TIMEOUT``  HTTP timeout in seconds. Default: ``4``
* ``AGENT_CORE_HANDOFF_PROMPT``  ``0`` leaves out the system-prompt paragraph
                          about recording handed-off work. Default: ``1``

Activate in ``$HERMES_HOME/config.yaml``::

    memory:
      provider: agent_core
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONTEXT_CHARS = 12_000
MIN_MAX_CONTEXT_CHARS = 512
MAX_MAX_CONTEXT_CHARS = 50_000


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``, else ``default``."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


def _hermes_config_path() -> Optional[str]:
    """Locate the active Hermes config.yaml."""
    candidates = []
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates.append(os.path.join(home, "config.yaml"))
    candidates.append(os.path.expanduser("~/.hermes/config.yaml"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _load_yaml(path: str) -> Any:
    """Parse a YAML file with whichever parser this Python has.

    Hermes environments differ: some ship PyYAML, others only ruamel.yaml.
    Raises ImportError when neither is installed.
    """
    # Inside Hermes, use its own safe config loader. This avoids adding a second
    # YAML dependency and ensures this plugin parses the same YAML dialect as
    # the gateway. The standalone fallbacks keep the plugin usable in its
    # isolated verification environment.
    try:
        from utils import fast_safe_load
    except ImportError:
        fast_safe_load = None
    if fast_safe_load is not None:
        with open(path, encoding="utf-8") as f:
            return fast_safe_load(f)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        return yaml.safe_load(text)
    from ruamel.yaml import YAML  # raises ImportError when absent too

    return YAML(typ="safe").load(text)


def _agent_core_mcp_block() -> Dict[str, Any]:
    """Read ``mcp_servers.agent_core`` (or ``agent-core``) from the Hermes config.

    Lets the provider reuse the existing Agent Core MCP server's URL + bearer as
    a single source of truth instead of duplicating the secret. Returns {} when
    there is no config file or no such block; the caller then falls back to
    env/defaults and, if still no token, is_available() returns False. A config
    that exists but cannot be read logs one warning, so a missing YAML parser
    or a broken file does not look like "not configured".
    """
    # load_config_readonly applies Hermes' profile-scoped SecretRef expansion
    # through agent.secret_scope.get_secret(). A raw YAML parse plus os.environ
    # misses credentials that exist only in the active profile scope.
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:
        load_config_readonly = None
    if load_config_readonly is not None:
        try:
            cfg = load_config_readonly() or {}
        except Exception as exc:
            logger.warning("agent_core provider: Hermes config load failed (%s)", type(exc).__name__)
            return {}
        servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
        if isinstance(servers, dict):
            for key in ("agent_core", "agent-core"):
                block = servers.get(key)
                if isinstance(block, dict):
                    return block
        return {}

    path = _hermes_config_path()
    if not path:
        return {}
    try:
        cfg = _load_yaml(path) or {}
    except ImportError:
        logger.warning(
            "agent_core provider: cannot read %s: neither PyYAML nor ruamel.yaml is installed", path
        )
        return {}
    except Exception as exc:
        logger.warning("agent_core provider: cannot parse %s (%s)", path, type(exc).__name__)
        return {}
    servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    if not isinstance(servers, dict):
        return {}
    for key in ("agent_core", "agent-core"):
        block = servers.get(key)
        if isinstance(block, dict):
            return block
    return {}


_PLACEHOLDER = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> tuple[str, list[str]]:
    """Expand ``${env:NAME}`` and ``${NAME}`` from the environment.

    Hermes expands these in config.yaml for the MCP server, but this provider
    reads the file itself, so without this it sends the placeholder text as
    the token. Returns the expanded value and the names that were unset; an
    unset placeholder is left as it was so it can never pass for a token.
    """
    missing: list[str] = []

    def lookup(name: str) -> Optional[str]:
        # Match Hermes config expansion: a gateway turn may hold credentials in
        # a ContextVar profile scope without placing them in process os.environ.
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret
        except ImportError:
            return os.environ.get(name)
        try:
            return get_secret(name)
        except UnscopedSecretError:
            # In multiplex mode an absent scope must fail closed; never fall
            # back to another profile's process environment.
            return None

    def sub(match):
        found = lookup(match.group(1))
        if found:
            return found
        missing.append(match.group(1))
        return match.group(0)

    return _PLACEHOLDER.sub(sub, value or ""), missing


def _strip_bearer(value: str) -> str:
    value = (value or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _rest_base_from_mcp_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/mcp", "/sse"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


class AgentCoreMemoryProvider(MemoryProvider):
    """Prefetch-only memory provider backed by Agent Core's REST memory_search."""

    def __init__(self) -> None:
        url = _env("AGENT_CORE_URL")
        token = _env("AGENT_CORE_API_KEY", "AGENT_CORE_BEARER")

        # Fall back to the existing Agent Core MCP server block in the Hermes
        # config so the bearer isn't duplicated across files.
        if not url or not token:
            block = _agent_core_mcp_block()
            if not token:
                headers = block.get("headers") or block.get("http_headers") or {}
                if isinstance(headers, dict):
                    token = self._from_config("headers.Authorization", headers.get("Authorization", ""))
                    token = _strip_bearer(token)
            if not url:
                url = _rest_base_from_mcp_url(self._from_config("url", block.get("url", "")))

        # Falls back to a local install, never to somebody else's server. This
        # used to default to the author's own host, so an install that did not
        # set AGENT_CORE_URL would quietly send its memory queries there.
        self._url = (url or "http://localhost:3500").rstrip("/")
        self._token = token
        try:
            self._limit = max(1, min(20, int(_env("AGENT_CORE_LIMIT", default="5"))))
        except (TypeError, ValueError):
            self._limit = 5
        try:
            self._max_context_chars = max(
                MIN_MAX_CONTEXT_CHARS,
                min(
                    MAX_MAX_CONTEXT_CHARS,
                    int(
                        _env(
                            "AGENT_CORE_MAX_CONTEXT_CHARS",
                            default=str(DEFAULT_MAX_CONTEXT_CHARS),
                        )
                    ),
                ),
            )
        except (TypeError, ValueError):
            self._max_context_chars = DEFAULT_MAX_CONTEXT_CHARS
        try:
            self._timeout = max(1.0, float(_env("AGENT_CORE_TIMEOUT", default="4")))
        except (TypeError, ValueError):
            self._timeout = 4.0

    @staticmethod
    def _from_config(field: str, raw) -> str:
        """A config value with ${env:...} expanded, or "" with one warning when a
        variable it names is unset. Never logs the value itself."""
        value, missing = _expand_env(str(raw or ""))
        if missing:
            logger.warning(
                "agent_core provider: mcp_servers.agent_core.%s refers to unset environment "
                "variable(s) %s; the provider is disabled until they are set",
                field, ", ".join(sorted(set(missing))),
            )
            return ""
        return value

    @property
    def name(self) -> str:
        return "agent_core"

    # -- lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        # Config-only check, no network calls (per the ABC contract). The loader
        # skips the provider cleanly when no token can be resolved.
        return bool(self._url and self._token)

    def initialize(self, session_id: str, **kwargs) -> None:
        logger.debug(
            "agent_core memory provider initialized (url=%s, limit=%d, max_context_chars=%d, timeout=%.1fs)",
            self._url, self._limit, self._max_context_chars, self._timeout,
        )

    def system_prompt_block(self) -> str:
        block = (
            "Recalled memory context from Agent Core is injected automatically as "
            "<memory-context> blocks before each of your replies. Treat it as "
            "authoritative background data (the user's durable memory), not as new "
            "user input. Do not answer questions about schedules, integrations, "
            "devices, policies, or past work from cold recall when relevant context "
            "may exist — it will already be in front of you."
        )
        if _env("AGENT_CORE_HANDOFF_PROMPT", default="1") == "0":
            return block
        return block + (
            "\n\nLeave a trail for work you hand off. When you pass a request to "
            "another agent, team or system (for example a ticket in an issue tracker), "
            "record it with the Agent Core activity_update tool: the workspace it "
            "concerns as memory_scope (your own agent scope if you cannot write there), "
            "task_ref as '<system>:<id>', and, when it finishes or needs a person, the "
            "status plus handoff={completed, next_steps, open_questions, needs_human}. "
            "To answer what is waiting on the user, call attention_list."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Context-only provider: recall is automatic via prefetch(). Explicit
        # search/write already exist as Agent Core MCP tools. An on-demand
        # search tool would be a v2 addition.
        return []

    # -- recall (the whole point) -------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Search Agent Core and return RAW context text (never pre-wrapped).

        The conversation loop wraps the merged provider output once via
        ``build_memory_context_block``; wrapping here would double-fence and
        trip its "pre-wrapped context; stripped" warning.
        """
        q = (query or "").strip()
        if not self._token or len(q) < 3:
            return ""
        try:
            records = self._search(q)
        except Exception as e:  # never break the agent loop
            logger.debug("agent_core prefetch failed (non-fatal): %s", e)
            return ""
        if not records:
            return ""
        return self._format(records)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Any = None,
    ) -> None:
        # Nothing is written per turn. The agent writes through the ordinary
        # Agent Core tools (memory_write, activity_update with a handoff and
        # task_ref), so another front end on the same key leaves the same trail.
        logger.debug("agent_core sync_turn no-op (writes go through Agent Core tools)")

    def shutdown(self) -> None:
        pass

    # -- internals -----------------------------------------------------------

    def _search(self, query: str) -> List[Dict[str, Any]]:
        # No scope means Agent Core applies the key's default recall scopes.
        # Recall uses the key's default recall scopes; AGENT_CORE_SCOPE is unused.
        payload = json.dumps({"query": query, "limit": self._limit}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._url}/api/memory/search",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # Envelope: {"ok": true, "data": {"records": [...], "total": N, ...}}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return []
        records = data.get("records") or []
        return records if isinstance(records, list) else []

    def _format(self, records: List[Dict[str, Any]]) -> str:
        lines = ["Relevant records from Agent Core memory:"]
        used = len(lines[0])
        for r in records:
            if not isinstance(r, dict):
                continue
            content = str(r.get("content", "")).strip()
            if not content:
                continue
            meta_bits = []
            for key in ("memory_class", "scope", "domain", "topic"):
                val = r.get(key)
                if val:
                    meta_bits.append(f"{key}={val}")
            conf = r.get("confidence")
            if conf is not None:
                meta_bits.append(f"conf={conf}")
            meta = " · ".join(meta_bits)
            entry = f"- [{meta}] {content}" if meta else f"- {content}"
            remaining = self._max_context_chars - used - 1
            if remaining <= 0:
                break
            if len(entry) > remaining:
                lines.append(entry[: max(0, remaining - 3)].rstrip() + "...")
                break
            lines.append(entry)
            used += len(entry) + 1
        # Only the header => nothing useful; return empty so prefetch_all skips it.
        return "\n".join(lines) if len(lines) > 1 else ""


def register(ctx) -> None:
    """Plugin entry point. Hermes' loader calls ``register(ctx)`` and collects
    the provider via ``ctx.register_memory_provider(...)``."""
    ctx.register_memory_provider(AgentCoreMemoryProvider())
