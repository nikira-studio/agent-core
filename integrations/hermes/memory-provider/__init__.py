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
* Read path only for v1. ``sync_turn()`` is a no-op; durable writes go through
  the Agent Core MCP ``memory_write`` tool, so there is no recall gap to close.
  (Write-back can be added in v2.)
* Fails safe. Any error in ``prefetch()`` returns ``""`` and logs at debug — a
  missing or broken Agent Core never breaks the agent loop. ``is_available()``
  returns ``False`` when no bearer token is configured, so the loader skips the
  provider cleanly.

Config (environment variables — keep secrets out of config.yaml)
----------------------------------------------------------------
* ``AGENT_CORE_URL``      Base URL of the Agent Core instance.
                          Default: ``http://core.veditz.com``
* ``AGENT_CORE_API_KEY``  Bearer token (required). ``AGENT_CORE_BEARER`` is
                          accepted as an alias.
* ``AGENT_CORE_SCOPE``    Reserved for v2 write-back. NOT used for recall —
                          prefetch searches every scope the token can read
                          (agent + owner user context + workspaces).
                          Default: ``agent:clawdia``
* ``AGENT_CORE_LIMIT``    Max records injected per turn. Default: ``5``
* ``AGENT_CORE_TIMEOUT``  HTTP timeout in seconds. Default: ``4``

Activate in ``$HERMES_HOME/config.yaml``::

    memory:
      provider: agent_core
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``, else ``default``."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


class AgentCoreMemoryProvider(MemoryProvider):
    """Prefetch-only memory provider backed by Agent Core's REST memory_search."""

    def __init__(self) -> None:
        self._url = _env("AGENT_CORE_URL", default="http://core.veditz.com").rstrip("/")
        self._token = _env("AGENT_CORE_API_KEY", "AGENT_CORE_BEARER")
        try:
            self._limit = max(1, min(20, int(_env("AGENT_CORE_LIMIT", default="5"))))
        except (TypeError, ValueError):
            self._limit = 5
        try:
            self._timeout = max(1.0, float(_env("AGENT_CORE_TIMEOUT", default="4")))
        except (TypeError, ValueError):
            self._timeout = 4.0

    @property
    def name(self) -> str:
        return "agent_core"

    # -- lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        # Config-only check, no network calls (per the ABC contract). The loader
        # skips the provider cleanly when there is no token.
        return bool(self._url and self._token)

    def initialize(self, session_id: str, **kwargs) -> None:
        logger.debug(
            "agent_core memory provider initialized (url=%s, limit=%d, timeout=%.1fs)",
            self._url, self._limit, self._timeout,
        )

    def system_prompt_block(self) -> str:
        return (
            "Recalled memory context from Agent Core is injected automatically as "
            "<memory-context> blocks before each of your replies. Treat it as "
            "authoritative background data (the user's durable memory), not as new "
            "user input. Do not answer questions about schedules, integrations, "
            "devices, policies, or past work from cold recall when relevant context "
            "may exist — it will already be in front of you."
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
        # v1: no write-back. Durable writes go through the Agent Core MCP
        # memory_write tool, so there is no recall gap to close here.
        logger.debug("agent_core sync_turn no-op (write-back is a v2 feature)")

    def shutdown(self) -> None:
        pass

    # -- internals -----------------------------------------------------------

    def _search(self, query: str) -> List[Dict[str, Any]]:
        # NB: no "scope" field => Agent Core searches every scope the token can
        # read (agent scope + owner user context + workspaces). That is exactly
        # what we want for recall. AGENT_CORE_SCOPE is only for v2 write-back.
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

    @staticmethod
    def _format(records: List[Dict[str, Any]]) -> str:
        lines = ["Relevant records from Agent Core memory:"]
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
            lines.append(f"- [{meta}] {content}" if meta else f"- {content}")
        # Only the header => nothing useful; return empty so prefetch_all skips it.
        return "\n".join(lines) if len(lines) > 1 else ""


def register(ctx) -> None:
    """Plugin entry point. Hermes' loader calls ``register(ctx)`` and collects
    the provider via ``ctx.register_memory_provider(...)``."""
    ctx.register_memory_provider(AgentCoreMemoryProvider())
