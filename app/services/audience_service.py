"""Who may use a connection: its audience list.

A connection (a capability set-up) has one owner scope (``connector_bindings.scope``: who may edit it) and an audience:
who may use it. An audience entry is one of
  * ``agent``         a named agent (``ref`` = agent id); with ``exclude`` set it is an exclusion: that agent may not use
                      the connection by any other entry; a named entry and an exclusion for the same agent cannot coexist;
  * ``workspace``     every agent that reaches that workspace (``ref`` = workspace id), by the same membership and
                      active-workspace rules as everywhere else;
  * ``person_agents`` "all my agents": every agent that acts for that person (``ref`` = user id).
Services are never in an audience: a service has no See or Run permission and fetches secrets through grants.

What an agent may then DO with a connection is unchanged: its See / Run permissions, the connection's action limits and
approvals. Being in the audience gives no memory or credential reach (that comes from workspaces and owners only).

Rows are written by the upgrade step that added the table (seeded from each connection's old scope, so nothing changes
on upgrade day), by creating or updating a connection with a bare ``scope`` (the old meaning: one audience), and by the
audience endpoints and dialogs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

from app.database import get_db
from app.time_utils import utc_now_iso

KINDS = ("agent", "workspace", "person_agents")


def table_exists(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_binding_audience'").fetchone())


def _json_list(value) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [s for s in parsed if isinstance(s, str)] if isinstance(parsed, list) else []


def principal_of(agent: dict) -> Optional[str]:
    return agent.get("default_user_id") or agent.get("owner_user_id")


# ── the rule ────────────────────────────────────────────────────────────────


def entry_matches(entry: dict, agent: dict, context) -> bool:
    """Does one non-exclusion entry name this agent (by itself, ignoring exclusions)?"""
    kind, ref = entry["kind"], entry["ref"]
    if entry.get("exclude"):
        return False
    if kind == "agent":
        return ref == agent["id"]
    if kind == "workspace":
        return f"workspace:{ref}" in set(context.read_scopes)
    if kind == "person_agents":
        # The person must be the one the agent acts for, and still active (a disabled person lends nothing).
        return context.user_id == ref and bool(context.principal_user_scope) and context.principal_user_scope in context.read_scopes
    return False


def may_use(entries: Iterable[dict], agent: dict, context) -> bool:
    """The audience rule for one agent and one connection's entries."""
    if (agent.get("kind") or "agent") != "agent":
        return False
    entries = list(entries)
    if any(e["kind"] == "agent" and e.get("exclude") and e["ref"] == agent["id"] for e in entries):
        return False
    return any(entry_matches(e, agent, context) for e in entries)


def all_entries(conn) -> dict[str, list[dict]]:
    """Every connection's audience, by connection id. Empty when the table does not exist yet."""
    if not table_exists(conn):
        return {}
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT binding_id, kind, ref, exclude FROM connector_binding_audience ORDER BY binding_id, kind, ref"
    ).fetchall():
        grouped.setdefault(row["binding_id"], []).append(
            {"kind": row["kind"], "ref": row["ref"], "exclude": bool(row["exclude"])}
        )
    return grouped


def usable_binding_ids(agent: dict, context) -> Optional[frozenset]:
    """The ids of the connections this agent may use, or None when audience lists do not exist yet (an upgrade step
    computing "before" on an older database)."""
    with get_db() as conn:
        if not table_exists(conn):
            return None
        grouped = all_entries(conn)
    return frozenset(bid for bid, entries in grouped.items() if may_use(entries, agent, context))


def entries_for(binding_id: str) -> list[dict]:
    with get_db() as conn:
        return all_entries(conn).get(binding_id, []) if table_exists(conn) else []


# ── seeding from the old single scope ──────────────────────────────────────


def seed_entries(conn, scope: str) -> list[dict]:
    """The audience that says what a connection owned by ``scope`` meant before audience lists: one entry (or a few
    plus exclusions) giving exactly the same agents the same reach. Used by the upgrade step and by any creation or
    update that sets only a ``scope``.

    The old rule: an agent could use a connection when its scope was one of the agent's read scopes, minus the agent's
    own person's personal scope unless the agent's ``inherit_user_bindings`` switch was on."""
    kind, _, ident = (scope or "").partition(":")
    agents = [dict(r) for r in conn.execute("SELECT * FROM agents WHERE COALESCE(kind, 'agent') = 'agent'").fetchall()] \
        if _has_column(conn, "agents", "kind") else [dict(r) for r in conn.execute("SELECT * FROM agents").fetchall()]
    if kind == "workspace":
        return [{"kind": "workspace", "ref": ident, "exclude": False}]
    if kind == "user":
        entries = [{"kind": "person_agents", "ref": ident, "exclude": False}]
        for agent in agents:
            if principal_of(agent) == ident:
                if not agent.get("inherit_user_bindings"):
                    entries.append({"kind": "agent", "ref": agent["id"], "exclude": True})
            elif scope in _json_list(agent.get("read_scopes_json")):
                # An agent acting for someone else that lists this person's scope itself reached it before.
                entries.append({"kind": "agent", "ref": agent["id"], "exclude": False})
        return entries
    # agent:<id> and any other scope (shared, system): the agents that reach it under the frozen 1.3 rule, that is by
    # their EFFECTIVE read scopes (what the old enforcement used), not by what is stored. Today the two agree for these
    # kinds (build_agent_context only adds the person's own scope and removes unreadable workspaces), but asking the same
    # question the "before" snapshot asks means a difference between them can never refuse an upgrade.
    from app.access_check import legacy_v13
    from app.database import adopt_connection
    from app.security.scope_enforcer import build_agent_context

    named = []
    with adopt_connection(conn):
        for agent in agents:
            if scope in legacy_v13.connector_read_scopes(build_agent_context(agent)):
                named.append(agent["id"])
    return [{"kind": "agent", "ref": a, "exclude": False} for a in sorted(named)]


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


# ── writing ─────────────────────────────────────────────────────────────────


def replace(conn, binding_id: str, entries: list[dict]) -> None:
    conn.execute("DELETE FROM connector_binding_audience WHERE binding_id = ?", (binding_id,))
    now = utc_now_iso()
    seen: set[tuple] = set()
    for e in entries:
        key = (e["kind"], e["ref"])
        if key in seen:
            continue  # one row per agent: a named entry and an exclusion cannot both exist
        seen.add(key)
        conn.execute(
            "INSERT INTO connector_binding_audience (binding_id, kind, ref, exclude, created_at) VALUES (?, ?, ?, ?, ?)",
            (binding_id, e["kind"], e["ref"], 1 if e.get("exclude") else 0, now),
        )


def seed(conn, binding_id: str, scope: str) -> None:
    """Replace a connection's audience with what its scope meant before audience lists."""
    if table_exists(conn):
        replace(conn, binding_id, seed_entries(conn, scope))


def validate(conn, entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Clean an audience sent by a person or a client: the entries that are fine, and plain messages for the rest."""
    clean, problems = [], []
    seen: dict[tuple, bool] = {}
    for raw in entries or []:
        kind, ref, exclude = raw.get("kind"), str(raw.get("ref") or "").strip(), bool(raw.get("exclude"))
        if kind not in KINDS or not ref:
            problems.append("Each entry needs a kind (agent, workspace or person_agents) and a name.")
            continue
        if exclude and kind != "agent":
            problems.append("Only an agent can be excluded.")
            continue
        if kind == "agent":
            row = conn.execute("SELECT id, kind FROM agents WHERE id = ?", (ref,)).fetchone()
            if not row:
                problems.append(f"There is no agent named {ref}.")
                continue
            if (row["kind"] if "kind" in row.keys() else "agent") == "service":
                problems.append(f"{ref} is a service. A service cannot use a capability; it fetches secrets.")
                continue
        elif kind == "workspace":
            if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (ref,)).fetchone():
                problems.append(f"There is no workspace named {ref}.")
                continue
        else:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (ref,)).fetchone():
                problems.append(f"There is no person named {ref}.")
                continue
        key = (kind, ref)
        if key in seen:
            if seen[key] != exclude:
                problems.append(f"{ref} is both added and excluded. Choose one.")
            continue
        seen[key] = exclude
        clean.append({"kind": kind, "ref": ref, "exclude": exclude})
    return clean, problems


def remove_references(conn, kind: str, ref: str) -> None:
    """A deleted agent, workspace or person leaves no audience row behind."""
    if table_exists(conn):
        conn.execute("DELETE FROM connector_binding_audience WHERE kind = ? AND ref = ?", (kind, ref))


def remove_for_bindings_of_scope(conn, scope: str) -> None:
    """Connections are deleted with their scope; their audience goes first (the foreign key would also cascade)."""
    if table_exists(conn):
        conn.execute(
            "DELETE FROM connector_binding_audience WHERE binding_id IN "
            "(SELECT id FROM connector_bindings WHERE scope = ? OR credential_id IN (SELECT id FROM credentials WHERE scope = ?))",
            (scope, scope),
        )


# ── the person's connections and the old per-agent switch ──────────────────


def person_audience_bindings(conn, person_id: str) -> list[str]:
    if not table_exists(conn):
        return []
    return [
        r["binding_id"] for r in conn.execute(
            "SELECT binding_id FROM connector_binding_audience WHERE kind = 'person_agents' AND ref = ?", (person_id,)
        ).fetchall()
    ]


def exclude_new_agent(conn, agent_id: str, person_id: Optional[str]) -> None:
    """A new agent starts without its person's connections (the old default): it is excluded from every
    connection whose audience says "all of this person's agents", until the person includes it."""
    set_agent_inherit(conn, agent_id, person_id, False)


def set_agent_inherit(conn, agent_id: str, person_id: Optional[str], inherit: bool) -> None:
    """The old per-agent switch in terms of the audience: off excludes the agent from its person's "all my agents"
    connections, on removes that exclusion. A named entry for the agent is left alone (it cannot coexist with one)."""
    if not person_id:
        return
    for binding_id in person_audience_bindings(conn, person_id):
        if inherit:
            conn.execute(
                "DELETE FROM connector_binding_audience WHERE binding_id = ? AND kind = 'agent' AND ref = ? AND exclude = 1",
                (binding_id, agent_id),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO connector_binding_audience (binding_id, kind, ref, exclude, created_at) VALUES (?, 'agent', ?, 1, ?)",
                (binding_id, agent_id, utc_now_iso()),
            )
