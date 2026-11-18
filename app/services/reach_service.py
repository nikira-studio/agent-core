"""Who can reach what, decided in one place.

Every page, every preview, the local credential helper and the upgrade check's access snapshot ask this module, so they
cannot disagree. It answers with the same enforcement objects the request path uses (``build_agent_context``,
``ScopeEnforcer``, ``EffectiveAuthority``); it adds no rule of its own except the plain-words reason for each answer.

Names and ids only. Nothing here reads a secret value.

Permission names used in answers are the stored names (``credentials``, ``connectors_execute``); ``PERMISSION_LABELS``
gives the words a person reads.
"""

from __future__ import annotations

from typing import Optional

from app.security.effective_authority import EffectiveAuthority
from app.security.scope_enforcer import ScopeEnforcer, build_agent_context
from app.services import agent_service

PERMISSION_LABELS = {
    "memory": "Use memory",
    "coordination": "Coordinate tasks",
    "credentials": "Reference credentials",
    "connectors_read": "See capabilities",
    "connectors_execute": "Run capabilities",
    "credentials_resolve": "Fetch secrets",
    "admin_propose": "Propose admin changes",
}


def _credential_enforcer(ctx) -> ScopeEnforcer:
    return ScopeEnforcer(
        ctx.read_scopes, ctx.write_scopes, ctx.agent_id,
        is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids,
    )


def active_agents() -> list[dict]:
    """Active entries that do agent work (services are not agents: they fetch through grants)."""
    return agent_service.list_agents(is_active=True, kind="agent")


class Names:
    """Plain names for workspaces and people, looked up once per call."""

    def __init__(self):
        from app.services import auth_service, workspace_service

        self._workspaces = {w["id"]: (w.get("name") or w["id"]) for w in (workspace_service.list_workspaces() or [])}
        self._users = {}
        self._auth = auth_service

    def workspace(self, workspace_id: str) -> str:
        return self._workspaces.get(workspace_id, workspace_id)

    def person(self, user_id: str) -> str:
        if user_id not in self._users:
            user = self._auth.get_user_by_id(user_id) if user_id else None
            self._users[user_id] = (user or {}).get("display_name") or user_id
        return self._users[user_id]

    def scope(self, scope: str) -> str:
        kind, _, ident = (scope or "").partition(":")
        if kind == "workspace":
            return f"workspace {self.workspace(ident)}"
        if kind == "user":
            return f"{self.person(ident)}'s personal space"
        if kind == "agent":
            return f"{ident}'s private space"
        if kind == "capability":
            from app.services import connector_service

            binding = connector_service.get_binding(ident)
            return f"connection {binding['name']}" if binding else "a deleted connection"
        return scope


def why(agent: dict, ctx, scope: str, names: Names) -> Optional[str]:
    """The plain-words reason this agent reaches ``scope``, or None when it does not (by the real scope rules)."""
    if not _credential_enforcer(ctx).can_read(scope):
        return None
    kind, _, ident = scope.partition(":")
    if scope == f"agent:{agent['id']}":
        return "owns it"
    if kind == "user" and ident == ctx.user_id:
        return f"acts for {names.person(ident)}"
    if kind == "workspace":
        return f"through workspace {names.workspace(ident)}"
    if scope == "shared":
        return "through shared access"
    return f"can read {names.scope(scope)}"


# ── credentials ────────────────────────────────────────────────────────────


def helper_decision(agent: Optional[dict], ctx, entry: dict) -> tuple[bool, str]:
    """May the local credential helper hand this credential to the agent it names? Returns (allowed, reason_code).

    The allow or deny is ``EffectiveAuthority.can("credential", "reference", ...)``, the very call the MCP
    ``credential_get`` path makes (the Reference credentials permission and the agent's scopes), on an active entry
    that does agent work, and the credential not expired. Only the reason code is worked out here, after a denial."""
    from app.services import credential_service

    if not agent or not agent.get("is_active"):
        return False, "agent_not_found"
    if (agent.get("kind") or "agent") != "agent":
        return False, "not_an_agent"
    authority = EffectiveAuthority(context=ctx)
    if not authority.can("credential", "reference", scope=entry["scope"]):
        return False, "needs_permission" if not authority.has_capability("credentials") else "scope_denied"
    if credential_service.is_expired(entry):
        return False, "expired"
    return True, "allowed"


def agents_reaching_credential(entry: dict) -> list[dict]:
    """Agents that reach this credential's owner scope, each with the reason and whether they can use the reference
    now (``can_reference``) or need the Reference credentials permission first (``needs_permission``)."""
    names = Names()
    result = []
    for agent in active_agents():
        ctx = build_agent_context(agent)
        reason = why(agent, ctx, entry["scope"], names)
        if reason is None:
            continue
        has = "credentials" in ctx.capabilities
        result.append({
            "agent_id": agent["id"],
            "display_name": agent.get("display_name") or agent["id"],
            "reason": reason,
            "status": "can_reference" if has else "needs_permission",
            "needs": None if has else "credentials",
            "needs_label": None if has else PERMISSION_LABELS["credentials"],
        })
    return sorted(result, key=lambda r: r["display_name"].lower())


# ── connections ────────────────────────────────────────────────────────────


def _audience_reason(entries: list[dict], agent: dict, ctx, names: "Names") -> str:
    """The plain-words reason an agent may use a connection: the first audience entry that names it."""
    from app.services import audience_service

    for e in entries:
        if not audience_service.entry_matches(e, agent, ctx):
            continue
        if e["kind"] == "agent":
            return "named on it"
        if e["kind"] == "workspace":
            return f"through workspace {names.workspace(e['ref'])}"
        return f"all of {names.person(e['ref'])}'s agents"
    return "in its audience"


def agents_using_connection(binding: dict) -> list[dict]:
    """Agents that may use this connection (by its audience), with the reason and whether they can run it now."""
    from app.services import audience_service

    names = Names()
    entries = audience_service.entries_for(binding["id"])
    result = []
    for agent in active_agents():
        ctx = build_agent_context(agent)
        authority = EffectiveAuthority(context=ctx)
        # The audience alone decides who is listed; the permission decides whether they can run it now.
        if binding["id"] not in (ctx.usable_binding_ids or frozenset()):
            continue
        can_run = authority.can_binding_action(binding["id"], "*", scope=binding["scope"])
        result.append({
            "agent_id": agent["id"],
            "display_name": agent.get("display_name") or agent["id"],
            "reason": _audience_reason(entries, agent, ctx, names),
            "status": "can_use" if can_run else "needs_permission",
            "needs": None if can_run else "connectors_execute",
            "needs_label": None if can_run else PERMISSION_LABELS["connectors_execute"],
        })
    return sorted(result, key=lambda r: r["display_name"].lower())


def connection_audience(binding: dict) -> list[dict]:
    """The audience list of a connection in plain words, for the dialogs: what each entry is and who it names."""
    from app.database import get_db
    from app.services import audience_service

    names = Names()
    with get_db() as conn:
        agents = {r["id"]: (r["display_name"] or r["id"]) for r in conn.execute("SELECT id, display_name FROM agents").fetchall()}
    rows = []
    for e in audience_service.entries_for(binding["id"]):
        if e["kind"] == "agent":
            label = agents.get(e["ref"], e["ref"])
            text = f"{label} is excluded" if e["exclude"] else label
        elif e["kind"] == "workspace":
            label, text = names.workspace(e["ref"]), f"Everyone in workspace {names.workspace(e['ref'])}"
        else:
            label, text = names.person(e["ref"]), f"All of {names.person(e['ref'])}'s agents"
        rows.append({**e, "label": label, "text": text})
    return rows


def preview_audience(binding: dict, entries: list[dict]) -> dict:
    """Who gains and who loses the use of a connection if its audience becomes ``entries``, with effective access."""
    from app.services import audience_service

    names = Names()
    current = audience_service.entries_for(binding["id"])
    gain, lose, keep = [], [], 0
    for agent in active_agents():
        ctx = build_agent_context(agent)
        was = audience_service.may_use(current, agent, ctx)
        now = audience_service.may_use(entries, agent, ctx)
        row = {"agent_id": agent["id"], "display_name": agent.get("display_name") or agent["id"]}
        if now and not was:
            can_run = "connectors_execute" in ctx.capabilities
            gain.append({**row, "reason": _audience_reason(entries, agent, ctx, names),
                         "status": "can_use" if can_run else "needs_permission",
                         "needs_label": None if can_run else PERMISSION_LABELS["connectors_execute"]})
        elif was and not now:
            lose.append({**row, "reason": _audience_reason(current, agent, ctx, names)})
        elif was and now:
            keep += 1
    return {
        "gain": sorted(gain, key=lambda r: r["display_name"].lower()),
        "lose": sorted(lose, key=lambda r: r["display_name"].lower()),
        "unchanged": keep,
    }


# ── one agent ──────────────────────────────────────────────────────────────


def agent_summary(agent: dict) -> dict:
    """What one agent can do and reach: workspaces, connections, credentials, keys. Effective access, not assignments."""
    from app.services import agent_access_service, connector_service, credential_service

    names = Names()
    ctx = build_agent_context(agent)
    authority = EffectiveAuthority(context=ctx)
    enforcer = _credential_enforcer(ctx)
    read = set(agent_service.parse_scopes(agent.get("read_scopes_json", "[]")))
    write = set(agent_service.parse_scopes(agent.get("write_scopes_json", "[]")))
    effective_read, effective_write = set(ctx.read_scopes), set(ctx.write_scopes)
    workspaces = []
    for scope in sorted(s for s in read | write if s.startswith("workspace:")):
        ident = scope.split(":", 1)[1]
        workspaces.append({
            "id": ident, "name": names.workspace(ident),
            "reads": scope in read, "writes": scope in write,
            "effective_read": scope in effective_read, "effective_write": scope in effective_write,
        })
    connections = []
    for binding in connector_service.list_bindings(enabled=True):
        if binding["id"] not in (ctx.usable_binding_ids or frozenset()):
            continue
        can_run = authority.can_binding_action(binding["id"], "*", scope=binding["scope"])
        connections.append({
            "id": binding["id"], "name": binding["name"], "capability": binding.get("connector_display_name"),
            "status": "can_use" if can_run else "needs_permission",
            "needs_label": None if can_run else PERMISSION_LABELS["connectors_execute"],
        })
    credentials = []
    has = "credentials" in ctx.capabilities
    for entry in credential_service.list_credentials(limit=1000):
        reason = why(agent, ctx, entry["scope"], names)
        if reason is None:
            continue
        credentials.append({
            "id": entry["id"], "name": entry["name"], "owner": names.scope(entry["scope"]), "reason": reason,
            "status": "can_reference" if has else "needs_permission",
            "needs_label": None if has else PERMISSION_LABELS["credentials"],
        })
    keys = agent_service.list_agent_keys(agent["id"])
    return {
        "agent": {"id": agent["id"], "display_name": agent.get("display_name") or agent["id"], "kind": agent.get("kind") or "agent"},
        "permissions": [{"name": p, "label": PERMISSION_LABELS[p]} for p in agent_service.parse_capabilities(agent.get("capabilities_json"))],
        "workspaces": workspaces,
        "no_effect": agent_access_service.no_effect(agent),
        "connections": connections,
        "credentials": sorted(credentials, key=lambda c: c["name"].lower()),
        "keys": [{"id": k["id"], "label": k["label"], "last_used_at": k.get("last_used_at")} for k in keys],
    }


# ── one service ────────────────────────────────────────────────────────────


def service_summary(service: dict) -> dict:
    """What one service may fetch (live, unexpired grants) and its keys."""
    from app.database import get_db
    from app.services import credential_service
    from app.services.credential_consumer_service import is_expired

    with get_db() as conn:
        rows = conn.execute(
            "SELECT g.id, g.entry_id, g.allow_write, g.expires_at, c.name, c.scope "
            "FROM credential_consumer_grants g JOIN credentials c ON c.id = g.entry_id "
            "WHERE g.consumer_agent_id = ? AND g.revoked_at IS NULL ORDER BY c.name",
            (service["id"],),
        ).fetchall()
    names = Names()
    grants = [
        {
            "grant_id": r["id"], "credential_id": r["entry_id"], "name": r["name"], "owner": names.scope(r["scope"]),
            "allow_write": bool(r["allow_write"]), "expires_at": r["expires_at"],
            "expired": is_expired({"expires_at": r["expires_at"]}),
        }
        for r in rows
    ]
    keys = agent_service.list_agent_keys(service["id"])
    return {
        "service": {"id": service["id"], "display_name": service.get("display_name") or service["id"]},
        "grants": grants,
        "keys": [{"id": k["id"], "label": k["label"], "last_used_at": k.get("last_used_at")} for k in keys],
        "all_credentials": [
            {"id": e["id"], "name": e["name"], "owner": names.scope(e["scope"])}
            for e in credential_service.list_credentials(limit=1000)
        ],
    }


# ── previews: effective access before anything is saved ────────────────────


def preview_credential_owner(entry: dict, new_scope: str) -> dict:
    """Who gains and who loses the ability to reach this credential if its owner becomes ``new_scope``.

    Connections that use it keep working (Agent Core uses the credential itself); services that were granted it keep
    their grant. Only agents that reach it by owner scope change."""
    names = Names()
    gain, lose, keep = [], [], 0
    for agent in active_agents():
        ctx = build_agent_context(agent)
        before = why(agent, ctx, entry["scope"], names)
        after = why(agent, ctx, new_scope, names)
        has = "credentials" in ctx.capabilities
        row = {"agent_id": agent["id"], "display_name": agent.get("display_name") or agent["id"]}
        if after and not before:
            gain.append({**row, "reason": after, "status": "can_reference" if has else "needs_permission"})
        elif before and not after:
            lose.append({**row, "reason": before})
        elif before and after:
            keep += 1
    return {
        "from": names.scope(entry["scope"]), "to": names.scope(new_scope),
        "gain": sorted(gain, key=lambda r: r["display_name"].lower()),
        "lose": sorted(lose, key=lambda r: r["display_name"].lower()),
        "unchanged": keep,
    }


def preview_agent_workspace(agent: dict, workspace_id: str, action: str, read: bool = True, write: bool = False) -> dict:
    """What adding (or removing) a workspace opens (or closes) for an agent: its memory, its credentials, its
    connections, with effective access (the person's membership and the agent's permissions applied)."""
    from app.services import connector_service, credential_service

    names = Names()
    scope = f"workspace:{workspace_id}"
    read_scopes = agent_service.parse_scopes(agent.get("read_scopes_json", "[]"))
    write_scopes = agent_service.parse_scopes(agent.get("write_scopes_json", "[]"))
    if action == "add":
        new_read = sorted(set(read_scopes) | ({scope} if read or write else set()))
        new_write = sorted(set(write_scopes) | ({scope} if write else set()))
    else:
        new_read = [s for s in read_scopes if s != scope]
        new_write = [s for s in write_scopes if s != scope]
    before_ctx = build_agent_context(agent)
    hypothetical = {**agent, "read_scopes_json": agent_service_json(new_read), "write_scopes_json": agent_service_json(new_write)}
    after_ctx = build_agent_context(hypothetical)
    authority = EffectiveAuthority(context=after_ctx)
    has_cred = "credentials" in after_ctx.capabilities
    credentials_changed = []
    # Whether the agent reaches the workspace's own scope before and after does not depend on the row.
    was = _credential_enforcer(before_ctx).can_read(scope)
    now = _credential_enforcer(after_ctx).can_read(scope)
    for entry in credential_service.list_credentials(scope=scope, limit=1000):
        if was != now:
            credentials_changed.append({
                "id": entry["id"], "name": entry["name"],
                "status": ("can_reference" if has_cred else "needs_permission") if now else "no_longer_reachable",
                "needs_label": None if (has_cred or not now) else PERMISSION_LABELS["credentials"],
            })
    connections_changed = []
    # A connection is gained or lost through the audience entries that name the agent (a workspace entry, mostly).
    for binding in connector_service.list_bindings(enabled=True):
        was = binding["id"] in (before_ctx.usable_binding_ids or frozenset())
        now = binding["id"] in (after_ctx.usable_binding_ids or frozenset())
        if was != now:
            can_run = authority.can_binding_action(binding["id"], "*", scope=binding["scope"])
            connections_changed.append({
                "id": binding["id"], "name": binding["name"],
                "status": ("can_use" if can_run else "needs_permission") if now else "no_longer_usable",
                "needs_label": None if (can_run or not now) else PERMISSION_LABELS["connectors_execute"],
            })
    memory_before = scope in before_ctx.read_scopes
    memory_after = scope in after_ctx.read_scopes
    stored_change = (scope in new_read) != (scope in read_scopes) or (scope in new_write) != (scope in write_scopes)
    return {
        "workspace": {"id": workspace_id, "name": names.workspace(workspace_id)},
        "action": action,
        "opens_memory": memory_after and not memory_before,
        "closes_memory": memory_before and not memory_after,
        "credentials": credentials_changed,
        "connections": connections_changed,
        # Saved but doing nothing yet: the agent's person is not a member of the workspace.
        "no_effect": stored_change and action == "add" and not memory_after,
    }


def agent_service_json(scopes: list[str]) -> str:
    import json

    return json.dumps(scopes)


# ── what the Credential, connection, agent and service pages show ──────────


def credential_access(entry: dict) -> dict:
    """The three groups of one credential's page: connections that sign in with it, agents that can reach it, and the
    services that receive its value, with what can be added to each."""
    from app.database import get_db
    from app.services import connector_service
    from app.services.credential_consumer_service import is_expired

    names = Names()
    bindings = connector_service.list_bindings()
    using = [b for b in bindings if b.get("credential_id") == entry["id"]]
    connections = [
        {
            "id": b["id"], "name": b["name"], "capability": b.get("connector_display_name"),
            "enabled": bool(b.get("enabled")), "owner": names.scope(b["scope"]),
            "users": agents_using_connection(b),
        }
        for b in using
    ]
    with get_db() as conn:
        grant_rows = conn.execute(
            "SELECT id, consumer_agent_id, allow_write, expires_at FROM credential_consumer_grants "
            "WHERE entry_id = ? AND revoked_at IS NULL ORDER BY created_at",
            (entry["id"],),
        ).fetchall()
    all_entries = {a["id"]: a for a in agent_service.list_agents(is_active=True)}
    held, granted = set(), []
    for g in grant_rows:
        if is_expired({"expires_at": g["expires_at"]}):
            continue
        held.add(g["consumer_agent_id"])
        holder = all_entries.get(g["consumer_agent_id"]) or {}
        granted.append({
            "grant_id": g["id"], "service_id": g["consumer_agent_id"],
            "display_name": holder.get("display_name") or g["consumer_agent_id"],
            "kind": holder.get("kind") or "agent",
            "allow_write": bool(g["allow_write"]), "expires_at": g["expires_at"],
        })
    can_receive = [
        {"id": a["id"], "display_name": a.get("display_name") or a["id"]}
        for a in all_entries.values()
        if (a.get("kind") == "service") and a["id"] not in held
    ]
    can_sign_in = [
        {"id": b["id"], "name": b["name"], "capability": b.get("connector_display_name")}
        for b in bindings if b.get("credential_id") != entry["id"]
    ]
    return {
        "credential": {"id": entry["id"], "name": entry["name"], "owner": names.scope(entry["scope"]), "scope": entry["scope"]},
        "connections": connections,
        "connections_to_add": can_sign_in,
        "agents": agents_reaching_credential(entry),
        "services": sorted(granted, key=lambda g: g["display_name"].lower()),
        "services_to_add": sorted(can_receive, key=lambda g: g["display_name"].lower()),
        # A grant held by an entry that is not a service (kept from before): listed so nobody is surprised.
        "held_by_agents": [g["display_name"] for g in granted if g["kind"] != "service"],
    }


def audience_options() -> dict:
    """What can be added to an audience: the agents (never services), the workspaces and the people, by plain name."""
    from app.services import auth_service, workspace_service

    return {
        "agents": [{"id": a["id"], "name": a.get("display_name") or a["id"]} for a in active_agents()],
        "workspaces": [
            {"id": w["id"], "name": w.get("name") or w["id"]}
            for w in (workspace_service.list_workspaces() or []) if w.get("is_active", True)
        ],
        "people": [
            {"id": u["id"], "name": u.get("display_name") or u["id"]}
            for u in auth_service.list_users() if u.get("is_active", True)
        ],
    }
