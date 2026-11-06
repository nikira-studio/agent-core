"""Access an agent was given but cannot use yet.

An agent acts for a person (its default user). Core only lets it use a
workspace that person can reach, so a workspace added to the agent's lists has
no effect until that person is a member. The lists look right either way, which
is how a grant ends up looking done and doing nothing; this finds those.
"""

from __future__ import annotations

from app.services import agent_service, auth_service, workspace_service


def no_effect(agent: dict) -> list[dict]:
    """Workspaces in the agent's lists that its person cannot reach, with why and a plain sentence."""
    read = set(agent_service.parse_scopes(agent.get("read_scopes_json", "[]")))
    write = set(agent_service.parse_scopes(agent.get("write_scopes_json", "[]")))
    workspace_ids = sorted(
        {s.split(":", 1)[1] for s in read | write if s.startswith("workspace:") and ":" in s}
    )
    if not workspace_ids:
        return []
    user_id = agent.get("default_user_id") or agent.get("owner_user_id")
    principal = auth_service.get_user_by_id(user_id) if user_id else None
    person = (principal or {}).get("display_name") or user_id or "its user"
    principal_active = bool(principal and principal.get("is_active", True))
    readable, writable = (
        workspace_service.get_workspace_authority(user_id, workspace_ids)
        if principal_active
        else (frozenset(), frozenset())
    )
    active = workspace_service.get_active_workspace_ids(workspace_ids)
    found = []
    for workspace_id in workspace_ids:
        scope = f"workspace:{workspace_id}"
        wants_read, wants_write = scope in read, scope in write
        workspace = workspace_service.get_workspace_by_id(workspace_id) or {}
        name = workspace.get("name") or workspace_id
        if workspace_id not in active:
            reason, sentence = "workspace_inactive", f"No effect: {name} is deactivated."
        elif not principal_active:
            reason, sentence = "person_disabled", f"No effect: {person} is disabled, so this agent cannot use {name}."
        elif (wants_read and workspace_id not in readable) or (wants_write and workspace_id not in writable):
            if workspace_id not in readable:
                reason = "not_member"
                sentence = f"No effect yet: {person} is not a member of {name}. Add them on Workspaces."
            else:
                reason = "read_only_member"
                sentence = f"No write access yet: {person} can read {name} but not write there. Change that on Workspaces."
        else:
            continue
        found.append({"workspace": workspace_id, "name": name, "person": person, "reason": reason, "message": sentence})
    return found
