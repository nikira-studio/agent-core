"""What each guarded schema revision declares about access (see guard.py)."""

from __future__ import annotations

from app.access_check.guard import RevisionCheck


def _kinds_match_the_rule(before: dict, after: dict) -> list[str]:
    problems = []
    for agent_id, was in before["identities"].items():
        now = after["identities"].get(agent_id)
        if now is None:
            problems.append(f"entry {agent_id} is missing after the step")
        elif now["kind"] != was["kind_by_rule"]:
            problems.append(f"entry {agent_id} stored as {now['kind']} but the rule says {was['kind_by_rule']}")
    for agent_id in after["identities"]:
        if agent_id not in before["identities"]:
            problems.append(f"entry {agent_id} appeared during the step")
    return problems


def _kept_as_agent(before: dict, after: dict) -> list[str]:
    """Entries whose permissions are exactly the service set but that stay agents (they have recorded work, workspaces
    or credentials of their own): listed for a person, never changed silently."""
    return [
        f"{agent_id} has only the service permission but stays an agent"
        for agent_id, item in after["identities"].items()
        if item["permissions"] == ["credentials_resolve"] and item["kind"] == "agent"
    ]


# Revision 30 changes how entries are listed, not what any of them may do: nothing is allowed to differ.
CHECKS: dict[int, RevisionCheck] = {
    30: RevisionCheck(allowed=(), validate=_kinds_match_the_rule, notes=_kept_as_agent),
    # Revision 31 adds the audience list. "Before" is every agent's reach under the frozen 1.3 rule; "after" is the new
    # rule over the seeded lists. Nothing may differ: not one agent x connection cell, see or run.
    31: RevisionCheck(allowed=(), before={"use_rule": "1.3"}),
}
