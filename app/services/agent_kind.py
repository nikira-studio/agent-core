"""Agent or service: what an entry on the Agents page is.

An *agent* does work: it records tasks, reads and writes memory, belongs to workspaces. A *service* is a program (usually
a container) that only fetches the secrets it was granted; it has one permission, ``credentials_resolve``, and nothing
else. The kind is stored on the entry (``agents.kind``) and decided once, by ``classify`` when the column is added or
when an entry is created; it is not recomputed later.

Everything here takes an open connection, so the schema revision that adds the column can use the same rules on the
connection that holds its uncommitted change.
"""

from __future__ import annotations

import json
from typing import Optional

KINDS = ("agent", "service")
SERVICE_PERMISSIONS = ["credentials_resolve"]


def has_service_permissions(capabilities_json: Optional[str]) -> bool:
    """True only when the stored permissions are exactly ``credentials_resolve`` (order and spacing do not matter).

    Anything that is not a list of text values (a malformed row) is not a service."""
    try:
        parsed = json.loads(capabilities_json or "[]")
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return False
    return len(parsed) == len(set(parsed)) and sorted(parsed) == SERVICE_PERMISSIONS


def _scopes(value: Optional[str]) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [s for s in parsed if isinstance(s, str)] if isinstance(parsed, list) else []


def _count(conn, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def recorded_work(conn, agent_id: str) -> list[str]:
    """What an entry has done or holds that a service would not: each item is one plain sentence.

    Checks the entry's recorded tasks, its sessions, memory written in its own scope, and the credentials and
    connections stored in its own scope."""
    found: list[str] = []
    own_scope = f"agent:{agent_id}"
    has_table = lambda name: conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if has_table("agent_activity"):
        n = _count(conn, "SELECT COUNT(*) FROM agent_activity WHERE agent_id = ? OR assigned_agent_id = ?", (agent_id, agent_id))
        if n:
            found.append(f"it has {n} recorded task{'s' if n != 1 else ''}")
    if has_table("agent_executions"):
        n = _count(conn, "SELECT COUNT(*) FROM agent_executions WHERE agent_id = ?", (agent_id,))
        if n:
            found.append(f"it has {n} recorded session{'s' if n != 1 else ''}")
    if has_table("memory_records"):
        n = _count(conn, "SELECT COUNT(*) FROM memory_records WHERE scope = ?", (own_scope,))
        if n:
            found.append(f"it has {n} memory record{'s' if n != 1 else ''} in its own scope")
    if has_table("credentials"):
        n = _count(conn, "SELECT COUNT(*) FROM credentials WHERE scope = ?", (own_scope,))
        if n:
            found.append(f"it owns {n} credential{'s' if n != 1 else ''}")
    if has_table("connector_bindings"):
        n = _count(conn, "SELECT COUNT(*) FROM connector_bindings WHERE scope = ?", (own_scope,))
        if n:
            found.append(f"it owns {n} connection{'s' if n != 1 else ''}")
    return found


def workspaces_of(agent: dict) -> list[str]:
    names = {
        s for s in _scopes(agent.get("read_scopes_json")) + _scopes(agent.get("write_scopes_json"))
        if s.startswith("workspace:")
    }
    return sorted(names)


def conflicts(conn, agent: dict) -> list[str]:
    """Why this entry cannot be a service, as plain sentences. Empty: nothing stands in the way."""
    found = recorded_work(conn, agent["id"])
    workspaces = workspaces_of(agent)
    if workspaces:
        found.append(f"it is in {len(workspaces)} workspace{'s' if len(workspaces) != 1 else ''}")
    return found


def classify(conn, agent: dict) -> str:
    """The kind an existing entry gets: a service only when its permissions are exactly ``credentials_resolve`` and
    nothing in ``conflicts`` says it does agent work."""
    if has_service_permissions(agent.get("capabilities_json")) and not conflicts(conn, agent):
        return "service"
    return "agent"


def normalize_kind(kind: Optional[str]) -> str:
    value = (kind or "agent").strip().lower()
    if value not in KINDS:
        raise ValueError("kind must be 'agent' or 'service'")
    return value


class AgentKindError(ValueError):
    """A change that would break the rules for services; the message is a plain sentence for the person."""


def check_service_shape(capabilities: Optional[list[str]], read_scopes: Optional[list[str]], write_scopes: Optional[list[str]],
                        agent_id: str, inherit_user_bindings: Optional[bool]) -> None:
    """A service has one permission and no workspaces. Raise AgentKindError for anything else."""
    if capabilities is not None and sorted(set(capabilities)) != SERVICE_PERMISSIONS:
        raise AgentKindError(
            "A service can only fetch secrets, so its one permission is credentials_resolve. "
            "To give it other permissions, change it to an agent first."
        )
    for scopes in (read_scopes, write_scopes):
        if scopes is not None and any(str(s).startswith("workspace:") for s in scopes):
            raise AgentKindError(
                "A service does not belong to workspaces. To give it one, change it to an agent first."
            )
    if inherit_user_bindings:
        raise AgentKindError("A service does not run capabilities, so it cannot take its person's capabilities.")
