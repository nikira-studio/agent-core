"""One table of every assignment, with when each was last used, and the list of what nothing uses.

The four assignments (see the access plan): a capability signs in with a credential; an agent may use a capability; an
agent can reach a credential (derived from its owner and workspaces); a service may fetch a credential. Everything comes
from ``reach_service``, the same answer the request path gives, plus the places that record use: connection runs, the
audit log and the daily resolution rollups. Names, ids, dates and yes/no only; no secret value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_db
from app.services import agent_service, connector_service, credential_service, reach_service
from app.services.credential_consumer_service import is_expired

# A service that has not signed in for this long is listed as idle.
IDLE_SERVICE_DAYS = 90

KINDS = {
    "capability_credential": "A capability signs in with a credential",
    "agent_capability": "An agent may use a capability",
    "agent_credential": "An agent can reach a credential",
    "service_credential": "A service may fetch a credential",
}


def _parse(value: Optional[str]) -> Optional[datetime]:
    """A stored time as an aware UTC instant. Stored times come in three shapes ("2026-11-02 09:00:00" in the audit log,
    "2026-11-17T00:00:00+00:00" on keys, "2026-11-02" in the daily rollups); comparing them as text is wrong on the
    boundary day."""
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T", 1) if len(str(value)) > 10 else str(value).strip() + "T00:00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _later(*values: Optional[str]) -> Optional[str]:
    present = [v for v in values if v]
    return max(present) if present else None


def _use_maps() -> dict:
    with get_db() as conn:
        runs = {
            (r["binding_id"], r["executor_agent_id"] or ""): r["last"]
            for r in conn.execute(
                "SELECT binding_id, executor_agent_id, MAX(executed_at) AS last FROM connector_executions "
                "GROUP BY binding_id, executor_agent_id"
            ).fetchall()
        }
        # The execution log is trimmed after its retention period; the audit log keeps a line for every run for longer,
        # so a connection that has not run lately is not shown as never used.
        for r in conn.execute(
            "SELECT resource_id, actor_type, actor_id, MAX(timestamp) AS last FROM audit_log "
            "WHERE action = 'connector_action_executed' GROUP BY resource_id, actor_type, actor_id"
        ).fetchall():
            key = (r["resource_id"], r["actor_id"] if r["actor_type"] == "agent" else "")
            runs[key] = _later(runs.get(key), r["last"])
        audit = {
            (r["actor_id"], r["resource_id"]): r["last"]
            for r in conn.execute(
                "SELECT actor_id, resource_id, MAX(timestamp) AS last FROM audit_log "
                "WHERE action IN ('credential_reference', 'broker_resolve', 'credential_consumer_resolved') "
                "GROUP BY actor_id, resource_id"
            ).fetchall()
        }
        rollup = {
            (r["actor_id"], r["credential_entry_id"]): r["last"]
            for r in conn.execute(
                "SELECT actor_id, credential_entry_id, MAX(resolution_date) AS last FROM credential_resolution_daily "
                "GROUP BY actor_id, credential_entry_id"
            ).fetchall()
        }
        keys = {
            r["agent_id"]: r["last"]
            for r in conn.execute(
                "SELECT agent_id, MAX(last_used_at) AS last FROM agent_keys WHERE revoked_at IS NULL GROUP BY agent_id"
            ).fetchall()
        }
        gates: dict[str, dict] = {}
        for r in conn.execute("SELECT connector_type_id, action FROM action_gates").fetchall():
            gates.setdefault(r["connector_type_id"], {})[r["action"]] = True
    return {"runs": runs, "audit": audit, "rollup": rollup, "keys": keys, "gates": gates}


def _last_credential_use(use: dict, actor_id: str, credential_id: str) -> Optional[str]:
    return _later(use["audit"].get((actor_id, credential_id)), use["rollup"].get((actor_id, credential_id)))


def overview() -> dict:
    use = _use_maps()
    agents = {a["id"]: a for a in agent_service.list_agents()}
    name_of = {i: (a.get("display_name") or i) for i, a in agents.items()}
    credentials = {e["id"]: e for e in credential_service.list_credentials(limit=100000)}
    bindings = connector_service.list_bindings()
    names = reach_service.Names()
    # Each agent's context is built once for the whole answer, not once per credential and per connection.
    contexts = reach_service.agent_contexts()
    rows: list[dict] = []

    def party(kind: str, ident: str, name: str) -> dict:
        return {"type": kind, "id": ident, "name": name}

    from app.services import audience_service

    with get_db() as conn:
        audiences = audience_service.all_entries(conn)

    # 1 and 2: connections, the credential each signs in with, and who can use each.
    connection_users: dict[str, list[dict]] = {}
    for b in bindings:
        gated = sorted(use["gates"].get(b["connector_type_id"], {}))
        last_run = _later(*[v for (bid, _), v in use["runs"].items() if bid == b["id"]])
        users = reach_service.agents_using_connection(b, contexts, names, audiences.get(b["id"], [])) if b.get("enabled") else []
        connection_users[b["id"]] = users
        capability = b.get("connector_display_name") or b["connector_type_id"]
        connection = party("connection", b["id"], reach_service.connection_label(capability, b["name"]))
        credential = credentials.get(b.get("credential_id") or "")
        if credential:
            rows.append({
                "assignment": "capability_credential", "from": connection,
                "to": party("credential", credential["id"], credential["name"]),
                "detail": "signs in with it", "last_used": last_run,
                "approval": gated, "owner": names.scope(credential["scope"]),
            })
        for u in users:
            rows.append({
                "assignment": "agent_capability", "from": party("agent", u["agent_id"], u["display_name"]),
                "to": connection, "detail": u["reason"], "status": u["status"], "needs_label": u.get("needs_label"),
                "last_used": use["runs"].get((b["id"], u["agent_id"])), "approval": gated,
            })

    # 3: agents that reach a credential through its owner and their workspaces.
    for credential in credentials.values():
        for r in reach_service.agents_reaching_credential(credential, contexts, names):
            rows.append({
                "assignment": "agent_credential", "from": party("agent", r["agent_id"], r["display_name"]),
                "to": party("credential", credential["id"], credential["name"]), "detail": r["reason"],
                "status": r["status"], "needs_label": r.get("needs_label"),
                "last_used": _last_credential_use(use, r["agent_id"], credential["id"]),
            })

    # 4: services that may fetch a credential.
    with get_db() as conn:
        grants = conn.execute(
            "SELECT id, consumer_agent_id, entry_id, allow_write, expires_at FROM credential_consumer_grants "
            "WHERE revoked_at IS NULL ORDER BY consumer_agent_id, entry_id"
        ).fetchall()
    for g in grants:
        credential = credentials.get(g["entry_id"])
        holder = agents.get(g["consumer_agent_id"]) or {}
        if not credential or is_expired({"expires_at": g["expires_at"]}):
            continue
        rows.append({
            "assignment": "service_credential",
            "from": party("service" if holder.get("kind") == "service" else "agent", g["consumer_agent_id"], name_of.get(g["consumer_agent_id"], g["consumer_agent_id"])),
            "to": party("credential", credential["id"], credential["name"]),
            "detail": ("read and write" if g["allow_write"] else "read only") + (f", until {g['expires_at'][:10]}" if g["expires_at"] else ""),
            "grant_id": g["id"], "last_used": _last_credential_use(use, g["consumer_agent_id"], g["entry_id"]),
        })

    return {"assignments": KINDS, "rows": rows, "unused": _unused(agents, credentials, bindings, connection_users, grants, use, names, audiences)}


def _unused(agents, credentials, bindings, connection_users, grants, use, names, audiences) -> dict:
    live = [g for g in grants if not is_expired({"expires_at": g["expires_at"]})]
    granted = {g["entry_id"] for g in live}
    signed_in = {b["credential_id"] for b in bindings if b.get("credential_id")}
    cutoff_at = datetime.now(timezone.utc) - timedelta(days=IDLE_SERVICE_DAYS)
    # The latest recorded reference or fetch of each credential by anyone (the audit log and the daily rollups).
    last_seen: dict[str, str] = {}
    for source in (use["audit"], use["rollup"]):
        for (_, credential_id), when in source.items():
            when_at = _parse(when)
            if when_at and (credential_id not in last_seen or when_at > _parse(last_seen[credential_id])):
                last_seen[credential_id] = when
    unused_credentials = []
    live_binding_ids = {b["id"] for b in bindings}
    orphaned = [
        c for c in credentials.values()
        if c["scope"].startswith("capability:") and c["scope"].split(":", 1)[1] not in live_binding_ids
    ]
    for c in credentials.values():
        if c["id"] in granted or c["id"] in signed_in or c in orphaned:
            continue
        seen = last_seen.get(c["id"])
        seen_at = _parse(seen)
        if seen_at and seen_at >= cutoff_at:
            continue  # nothing is set up to use it, but something used it recently: it is in use
        unused_credentials.append({
            "id": c["id"], "name": c["name"], "owner": names.scope(c["scope"]), "last_referenced": seen,
            "why": "No connection signs in with it and no service may fetch it. "
                   + (f"Last referenced on {seen_at.strftime('%-d %b %Y')}." if seen_at else "Never referenced."),
        })
    unusable = [
        {"id": b["id"], "name": reach_service.connection_label(b.get("connector_display_name") or b["connector_type_id"], b["name"]),
         "why": "It is switched off." if not b.get("enabled") else "No agent can use it."}
        for b in bindings if not b.get("enabled") or not connection_users.get(b["id"])
    ]
    holders = {g["consumer_agent_id"] for g in live}
    idle = []
    for a in agents.values():
        if a.get("kind") != "service" or not a.get("is_active"):
            continue
        last = use["keys"].get(a["id"])
        last_at = _parse(last)
        if last_at is None or last_at < cutoff_at:
            idle.append({
                "id": a["id"], "name": a.get("display_name") or a["id"], "last_signed_in": last,
                "why": f"It has not signed in for {IDLE_SERVICE_DAYS} days." if last else "It has never signed in.",
            })
        elif a["id"] not in holders:
            idle.append({"id": a["id"], "name": a.get("display_name") or a["id"], "last_signed_in": last,
                         "why": "It may fetch no secret."})
    left_behind = []
    for a in agents.values():
        if a.get("is_active"):
            continue  # a retired agent or a retired service
        own = f"agent:{a['id']}"
        held = [g for g in live if g["consumer_agent_id"] == a["id"]]
        owned_credentials = [c["name"] for c in credentials.values() if c["scope"] == own]
        owned_connections = [b["name"] for b in bindings if b["scope"] == own]
        named_on = [
            b["name"] for b in bindings
            if any(e["kind"] == "agent" and e["ref"] == a["id"] and not e["exclude"] for e in audiences.get(b["id"], []))
        ]
        if held or owned_credentials or owned_connections or named_on:
            parts = []
            if held:
                parts.append(f"{len(held)} secret{'s' if len(held) != 1 else ''} it may still fetch")
            if owned_credentials:
                parts.append(f"credentials it owns: {', '.join(owned_credentials)}")
            if owned_connections:
                parts.append(f"connections it owns: {', '.join(owned_connections)}")
            if named_on:
                parts.append(f"named as a user of: {', '.join(named_on)}")
            left_behind.append({"id": a["id"], "name": a.get("display_name") or a["id"], "why": "; ".join(parts)})
    for c in orphaned:
        holders = sorted(
            (agents.get(g["consumer_agent_id"]) or {}).get("display_name") or g["consumer_agent_id"]
            for g in live if g["entry_id"] == c["id"]
        )
        left_behind.append({
            "id": c["id"], "name": c["name"],
            "why": "It belonged to a connection that was deleted, and no agent can reach it."
                   + (f" Services that still receive it: {', '.join(holders)}." if holders else ""),
        })
    return {
        "credentials": sorted(unused_credentials, key=lambda r: r["name"].lower()),
        "connections": sorted(unusable, key=lambda r: r["name"].lower()),
        "services": sorted(idle, key=lambda r: r["name"].lower()),
        "left_behind": sorted(left_behind, key=lambda r: r["name"].lower()),
        "idle_days": IDLE_SERVICE_DAYS,
    }
