"""A snapshot of effective access: who can do or reach what, by the code that enforces it.

Names, ids and yes/no answers only. No secret value is read: credentials are recorded by id, scope, name, reference
name and a hash of the stored (encrypted) value, which is enough to see that a row has not moved or changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.database import adopt_connection

SNAPSHOT_VERSION = 1


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def capture(conn) -> dict:
    """Snapshot the installation as ``conn`` sees it (including uncommitted changes on that connection)."""
    with adopt_connection(conn):
        return _capture(conn)


def _capture(conn) -> dict:
    from app.security.effective_authority import EffectiveAuthority
    from app.security.scope_enforcer import ScopeEnforcer, build_agent_context
    from app.services import agent_kind, agent_service, credential_service

    agent_columns = _columns(conn, "agents")
    stored_kind = "kind" in agent_columns

    credentials: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT id, scope, name, reference_name, value_encrypted, expires_at FROM credentials ORDER BY id"
    ).fetchall():
        credentials[row["id"]] = {
            "scope": row["scope"],
            "name": row["name"],
            "reference_name": row["reference_name"],
            "value_sha256": hashlib.sha256((row["value_encrypted"] or "").encode()).hexdigest(),
            "expires_at": row["expires_at"],
            "expired": credential_service.is_expired({"id": row["id"], "expires_at": row["expires_at"]}),
        }

    binding_columns = (
        "connector_type_id", "name", "scope", "credential_id", "config_json", "rate_limit_config_json",
        "logical_alias", "is_preferred", "priority", "endpoint_url_override", "enabled", "actions_mode",
        "actions_disabled_json",
    )
    present = _columns(conn, "connector_bindings")
    bindings: dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM connector_bindings ORDER BY id").fetchall():
        data = {c: row[c] for c in binding_columns if c in present}
        bindings[row["id"]] = {
            "scope": row["scope"],
            "credential_id": row["credential_id"],
            "enabled": bool(row["enabled"]),
            "digest": _digest(data),
        }

    live_keys: dict[str, list[str]] = {}
    if _has_table(conn, "agent_keys"):
        for row in conn.execute(
            "SELECT agent_id, key_hash FROM agent_keys WHERE revoked_at IS NULL ORDER BY agent_id, key_hash"
        ).fetchall():
            live_keys.setdefault(row["agent_id"], []).append(row["key_hash"])

    identities: dict[str, dict] = {}
    see: dict[str, list[str]] = {}
    run: dict[str, list[str]] = {}
    mcp_reference: dict[str, list[str]] = {}
    helper_fetch: dict[str, list[str]] = {}

    agent_rows = conn.execute(
        "SELECT * FROM agents ORDER BY id"
    ).fetchall()
    for row in agent_rows:
        agent = dict(row)
        agent_id = agent["id"]
        rule_kind = agent_kind.classify(conn, agent)
        identities[agent_id] = {
            "active": bool(agent["is_active"]),
            "permissions": agent_service.parse_capabilities(agent.get("capabilities_json")),
            "person": agent.get("default_user_id") or agent.get("owner_user_id"),
            "inherit_user_bindings": bool(agent.get("inherit_user_bindings")),
            "live_keys": len(live_keys.get(agent_id, [])),
            "keys_digest": _digest(live_keys.get(agent_id, [])),
            # What the entry may reach through memory and shared areas, active or not.
            "read_scopes": sorted(set(agent_kind._scopes(agent.get("read_scopes_json")))),
            "write_scopes": sorted(set(agent_kind._scopes(agent.get("write_scopes_json")))),
            "recall_scopes": sorted(set(agent_kind._scopes(agent.get("default_recall_scopes_json")))),
            "kind": agent.get("kind") if stored_kind else rule_kind,
            "kind_by_rule": rule_kind,
        }
        if not agent["is_active"]:
            continue
        context = build_agent_context(agent)
        authority = EffectiveAuthority(context=context)
        enforcer = ScopeEnforcer(
            context.read_scopes, context.write_scopes, context.agent_id,
            is_admin=context.is_admin, active_workspace_ids=context.active_workspace_ids,
        )
        see[agent_id] = sorted(
            bid for bid, b in bindings.items()
            if b["enabled"] and authority.can("connector", "read", scope=b["scope"])
        )
        run[agent_id] = sorted(
            bid for bid, b in bindings.items()
            if b["enabled"] and authority.can_binding_action(bid, "*", scope=b["scope"])
        )
        mcp_reference[agent_id] = sorted(
            cid for cid, c in credentials.items()
            if not c["expired"] and authority.can("credential", "reference", scope=c["scope"])
        )
        # The local credential helper (/internal/credentials/resolve) checks scope and expiry only.
        helper_fetch[agent_id] = sorted(
            cid for cid, c in credentials.items() if not c["expired"] and enforcer.can_read(c["scope"])
        )

    fetch: dict[str, dict] = {}
    if _has_table(conn, "credential_consumer_grants"):
        for row in conn.execute(
            "SELECT consumer_agent_id, entry_id, allow_write, expires_at FROM credential_consumer_grants "
            "WHERE revoked_at IS NULL ORDER BY consumer_agent_id, entry_id"
        ).fetchall():
            fetch.setdefault(row["consumer_agent_id"], {})[row["entry_id"]] = {
                "write": bool(row["allow_write"]),
                "expires_at": row["expires_at"],
            }

    return {
        "version": SNAPSHOT_VERSION,
        "identities": identities,
        "credentials": credentials,
        "bindings": bindings,
        "see": see,
        "run": run,
        "mcp_reference": mcp_reference,
        "helper_fetch": helper_fetch,
        "fetch": fetch,
    }


def counts(snapshot: dict) -> dict:
    identities = snapshot["identities"]
    return {
        "identities": len(identities),
        "agents": sum(1 for i in identities.values() if i["kind"] == "agent"),
        "services": sum(1 for i in identities.values() if i["kind"] == "service"),
        "bindings": len(snapshot["bindings"]),
        "credentials": len(snapshot["credentials"]),
        "grants": sum(len(v) for v in snapshot["fetch"].values()),
    }
