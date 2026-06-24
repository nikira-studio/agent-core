"""Tool-result offloader storage.

Large MCP tool outputs are written here and replaced in the response with a
compact summary plus a handle. The agent retrieves the full payload in slices
via the result_fetch tool only when it actually needs it, keeping big payloads
(broad connector listings, full-scope memory dumps, large HTTP bodies) out of
the active context window.

Storage is a single SQLite table riding the existing DB, backup, and
purge machinery. Expired rows are swept lazily on each write, matching the
connector_oauth_states pattern (no separate scheduler).
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from app.database import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summarize(content: str, preview_chars: int = 500) -> dict:
    """Cheap, deterministic summary of a spilled payload. No LLM calls.

    Adds a structural hint (top-level JSON keys, or list length) when the
    content parses as JSON, so the agent can decide what to fetch.
    """
    preview = content[:preview_chars]
    summary = {
        "preview": preview,
        "preview_truncated": len(content) > preview_chars,
    }
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return summary
    if isinstance(parsed, dict):
        summary["structure"] = "object"
        summary["top_level_keys"] = list(parsed.keys())[:50]
    elif isinstance(parsed, list):
        summary["structure"] = "array"
        summary["item_count"] = len(parsed)
    return summary


def cleanup_expired(conn=None) -> int:
    """Delete spilled payloads past their expiry. Returns rows removed."""
    def _run(c):
        return c.execute(
            "DELETE FROM tool_result_spill WHERE expires_at IS NOT NULL AND expires_at < ?",
            (_now().isoformat(),),
        ).rowcount

    if conn is not None:
        return _run(conn)
    with get_db() as c:
        return _run(c)


def spill(agent_id: str | None, tool: str | None, content: str, ttl_hours: int) -> dict:
    """Persist a large tool payload and return a handle + summary.

    The returned dict is what gets surfaced to the agent in place of the raw
    payload.
    """
    handle = secrets.token_urlsafe(16)
    total_chars = len(content)
    expires_at = (_now() + timedelta(hours=ttl_hours)).isoformat() if ttl_hours > 0 else None
    with get_db() as conn:
        cleanup_expired(conn)
        conn.execute(
            """
            INSERT INTO tool_result_spill
                (id, agent_id, tool, content, total_chars, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (handle, agent_id, tool, content, total_chars, expires_at),
        )
    summary = _summarize(content)
    return {
        "offloaded": True,
        "reason": "Result exceeded the inline size threshold and was offloaded to "
        "keep it out of the context window.",
        "handle": handle,
        "total_chars": total_chars,
        "expires_at": expires_at,
        "summary": summary,
        "retrieve_with": {
            "tool": "result_fetch",
            "params": {"handle": handle, "offset": 0, "limit": 4000},
        },
    }


def fetch(handle: str, offset: int, limit: int, agent_id: str | None = None) -> dict | None:
    """Return a slice of a spilled payload, or None if missing/expired.

    When agent_id is provided the lookup is scoped to that agent so one agent
    cannot read another's offloaded payloads.
    """
    with get_db() as conn:
        cleanup_expired(conn)
        if agent_id is not None:
            row = conn.execute(
                "SELECT id, tool, content, total_chars, expires_at FROM tool_result_spill "
                "WHERE id = ? AND (agent_id = ? OR agent_id IS NULL)",
                (handle, agent_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, tool, content, total_chars, expires_at FROM tool_result_spill "
                "WHERE id = ?",
                (handle,),
            ).fetchone()
    if row is None:
        return None

    total = row["total_chars"]
    offset = max(0, offset)
    limit = max(1, limit)
    chunk = row["content"][offset : offset + limit]
    next_offset = offset + len(chunk)
    return {
        "handle": handle,
        "tool": row["tool"],
        "total_chars": total,
        "offset": offset,
        "returned_chars": len(chunk),
        "next_offset": next_offset if next_offset < total else None,
        "has_more": next_offset < total,
        "content": chunk,
    }
