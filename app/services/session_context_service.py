"""Session-start context: one budgeted block an agent receives without asking.

Agents were told to open every session with workspace_sync, activity_update and
two or three memory searches. They did so unevenly, and when they did, the sync
payload was often too big to deliver inline. This builds the same orientation
server-side, from one call a client hook can make before the model's first turn:

- standing rules (pinned records),
- work that is unfinished or was handed to this agent,
- the most recent work in the workspace, with its result,
- what changed since this agent's last session, one line per resource,
- an index of the workspace's key records: ids and a first line, not bodies.

The block is fitted to a character budget by dropping whole items in a fixed
order, cheapest loss first, and never cut mid-item. Claude Code replaces any
hook output over 10,000 characters with a short stub, so an unfitted block
would deliver nothing at all.

What was injected is recorded (audit action context_injected), so the value
report can tell whether an injected record is later fetched.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from app.branding import APP_NAME
from app.database import get_db
from app.services import audit_service, workspace_sync_service

DEFAULT_BUDGET_CHARS = 9000
MAX_BUDGET_CHARS = 50000
MIN_BUDGET_CHARS = 1000

_INDEX_LIMIT = 40
_INDEX_FRESH_DAYS = 14
_INDEX_RECALLED_DAYS = 30
_CHANGE_LIMIT = 50
_RECENT_LIMIT = 4
_ATTENTION_LIMIT = 6


def _one_line(text: Optional[str], limit: int) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _workspace_roots() -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'workspace_repo_roots'"
        ).fetchone()
    try:
        roots = json.loads(row["value"]) if row and row["value"] else {}
    except (TypeError, ValueError):
        return {}
    return roots if isinstance(roots, dict) else {}


def resolve_scope(
    readable_scopes: list[str], requested: Optional[str], cwd: Optional[str]
) -> tuple[Optional[str], str]:
    """Pick the workspace for this session and say how it was chosen.

    An explicit scope wins. Otherwise the client's working directory is matched
    against the configured workspace repo roots, deepest root first, so a hook
    needs no per-repo configuration. Only scopes the caller can read qualify.
    """
    readable = set(readable_scopes)
    if requested:
        return (requested, "requested") if requested in readable else (None, "denied")
    if cwd:
        path = os.path.normpath(cwd)
        matches = []
        for scope, root in _workspace_roots().items():
            if scope not in readable or not isinstance(root, str) or not root:
                continue
            root = os.path.normpath(root)
            if path == root or path.startswith(root + os.sep):
                matches.append((len(root), scope))
        if matches:
            return max(matches)[1], "cwd"
    workspaces = sorted(s for s in readable if s.startswith("workspace:"))
    if len(workspaces) == 1:
        return workspaces[0], "only_workspace"
    return None, "unmatched"


def _pinned(conn, scope: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, content, memory_class, topic FROM memory_records
            WHERE scope = ? AND pinned = 1 AND record_status = 'active'
            ORDER BY created_at""",
            (scope,),
        ).fetchall()
    ]


def _attention(conn, scope: str, agent_id: str) -> list[dict]:
    """Unfinished work of this agent's, and work someone handed to it."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, agent_id, task_description, task_note, status, updated_at, handoff_json
            FROM agent_activity
            WHERE memory_scope = ? AND assigned_agent_id = ?
              AND status IN ('active', 'stale', 'blocked')
            ORDER BY updated_at DESC LIMIT ?""",
            (scope, agent_id, _ATTENTION_LIMIT),
        ).fetchall()
    ]


def _recent(conn, scope: str, exclude_ids: set[str]) -> list[dict]:
    rows = conn.execute(
        """SELECT id, agent_id, task_description, task_note, task_result, status, handoff_json,
                  COALESCE(ended_at, updated_at) AS at
        FROM agent_activity
        WHERE memory_scope = ? AND status IN ('completed', 'blocked')
        ORDER BY COALESCE(ended_at, updated_at) DESC LIMIT ?""",
        (scope, _RECENT_LIMIT + len(exclude_ids)),
    ).fetchall()
    return [dict(r) for r in rows if r["id"] not in exclude_ids][:_RECENT_LIMIT]


def _changes(scope: str, agent_id: str, shown_activity_ids: set[str]) -> list[dict]:
    """Changes since this agent's last activity here, latest state per resource."""
    from app.services import activity_service

    prior = activity_service.get_last_activity_in_scope(agent_id, scope)
    digest = workspace_sync_service.digest_for_new_arrival(
        scope, prior["id"] if prior else None, new_activity_id="", limit=_CHANGE_LIMIT
    )
    folded: dict = {}
    for change in digest["changes"]:  # newest first
        key = (change["resource_type"], change["resource_id"])
        if key in folded or change["resource_id"] in shown_activity_ids:
            continue
        folded[key] = change
    return list(folded.values())


def _index(conn, scope: str, exclude_ids: set[str]) -> tuple[list[dict], int]:
    """Records in current use first: written or confirmed lately, then recalled
    lately, then the rest. Within a tier decisions lead, since a constraint is
    the most expensive thing to rediscover. Old records nobody touches fall
    off the list; a rule that must always show belongs in pinned."""
    rows = conn.execute(
        f"""SELECT id, content, memory_class, topic FROM memory_records
        WHERE scope = ? AND record_status = 'active' AND pinned = 0
          AND memory_class IN ('decision', 'fact', 'preference')
          AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
        ORDER BY CASE
                   WHEN datetime(MAX(created_at, COALESCE(last_confirmed_at, '')))
                        >= datetime('now', '-{_INDEX_FRESH_DAYS} days') THEN 0
                   WHEN datetime(last_recalled_at)
                        >= datetime('now', '-{_INDEX_RECALLED_DAYS} days') THEN 1
                   ELSE 2
                 END,
                 (memory_class = 'decision') DESC,
                 COALESCE(helpful_count, 0) - COALESCE(unhelpful_count, 0) DESC,
                 COALESCE(recall_count, 0) DESC,
                 created_at DESC
        LIMIT {_INDEX_LIMIT + len(exclude_ids)}"""
        ,
        (scope,),
    ).fetchall()
    total = conn.execute(
        """SELECT COUNT(*) FROM memory_records WHERE scope = ? AND record_status = 'active'
        AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))""",
        (scope,),
    ).fetchone()[0]
    return [dict(r) for r in rows if r["id"] not in exclude_ids][:_INDEX_LIMIT], total


def _handoff(activity: dict) -> dict:
    try:
        value = json.loads(activity.get("handoff_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _needs_human(conn, scope: str) -> list[dict]:
    """Handoffs waiting on a person, newest first: what the human must do."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, agent_id, task_description, handoff_json FROM agent_activity
            WHERE memory_scope = ? AND status IN ('blocked', 'completed', 'active', 'stale')
              AND json_valid(handoff_json)
              AND COALESCE(json_extract(handoff_json, '$.needs_human'), '') != ''
              AND handoff_resolved_at IS NULL
            ORDER BY updated_at DESC LIMIT 5""",
            (scope,),
        ).fetchall()
    ]


def _render(scope: str, execution_id: str, parts: dict, limits: dict) -> str:
    lines = [
        f"# {APP_NAME} session context: {scope}",
        f"execution_id: {execution_id} — pass it to activity_update and memory_write. "
        "This replaces the startup workspace_sync. Begin each task with "
        f"activity_update(status=active, memory_scope={scope}).",
    ]

    pinned = parts["pinned"]
    if pinned:
        lines += ["", "## Standing rules (pinned)"]
        for record in pinned:
            lines.append(f"- {_one_line(record['content'], limits['pinned_chars'])}")

    waiting = parts.get("needs_human") or []
    if waiting:
        lines += ["", "## Waiting on a person"]
        for activity in waiting:
            lines.append(
                f"- {activity['id']} ({activity['agent_id']}): "
                f"{_one_line(_handoff(activity).get('needs_human'), 240)}"
            )

    attention = parts["attention"]
    if attention:
        lines += ["", "## Unfinished or handed to you"]
        for activity in attention:
            owner = "" if activity["agent_id"] == parts["agent_id"] else f" (from {activity['agent_id']})"
            note = _one_line(activity["task_note"], 240)
            steps = _handoff(activity).get("next_steps") or []
            if steps:
                note = "next: " + _one_line("; ".join(steps), 240)
            lines.append(
                f"- [{activity['status']}] {activity['id']}{owner}: "
                f"{_one_line(activity['task_description'], 200)}" + (f" — last note: {note}" if note else "")
            )

    recent = parts["recent"][: limits["recent"]]
    if recent:
        lines += ["", "## Recent work in this workspace"]
        for position, activity in enumerate(recent):
            detail_limit = limits["recent_lead_chars"] if position == 0 else limits["recent_chars"]
            outcome = activity["task_result"] or activity["task_note"]
            lines.append(
                f"- {str(activity['at'])[:10]} {activity['agent_id']} {activity['status']}: "
                f"{_one_line(activity['task_description'], 160)}"
                + (f" → {_one_line(outcome, detail_limit)}" if outcome and detail_limit else "")
            )
            handoff = _handoff(activity)
            if handoff.get("next_steps") and detail_limit:
                lines.append(f"  next steps: {_one_line('; '.join(handoff['next_steps']), detail_limit)}")
            if handoff.get("open_questions") and detail_limit:
                lines.append(f"  open questions: {_one_line('; '.join(handoff['open_questions']), detail_limit)}")

    changes = parts["changes"]
    shown_changes = changes[: limits["changes"]]
    if changes:
        lines += ["", f"## Changed since your last session ({len(changes)})"]
        for change in shown_changes:
            summary = change.get("summary") or {}
            label = summary.get("topic") or _one_line(summary.get("task_description"), 80)
            preview = summary.get("preview") or summary.get("task_result") or summary.get("task_note")
            by = change.get("source_agent_id") or "?"
            lines.append(
                f"- {change['change_type']} {change['resource_id']} by {by}"
                + (f" · {label}" if label else "")
                + (f": {_one_line(preview, 140)}" if preview else "")
            )
        if len(changes) > len(shown_changes):
            lines.append(f"- … {len(changes) - len(shown_changes)} more: workspace_sync(execution_id) lists them")

    index = parts["index"][: limits["index"]]
    if parts["index_total"]:
        lines += ["", f"## Key records ({len(index)} of {parts['index_total']} active)"]
        for record in index:
            topic = f" · {record['topic']}" if record.get("topic") else ""
            lines.append(
                f"- {record['id']} {record['memory_class']}{topic}: {_one_line(record['content'], 140)}"
            )
        lines.append(
            "Full text: memory_get(ids=[...]). Anything else: memory_search(query)."
        )
    return "\n".join(lines)


# The order things are given up in, cheapest loss first. Each step returns the
# next limits, or None when nothing more can go.
def _reduce(limits: dict, parts: dict) -> Optional[dict]:
    if limits["index"] > 10:
        return {**limits, "index": max(10, limits["index"] // 2)}
    if limits["changes"] > 5:
        return {**limits, "changes": max(5, limits["changes"] // 2)}
    if limits["recent_chars"] > 0:
        return {**limits, "recent_chars": 0}
    if limits["recent_lead_chars"] > 200:
        return {**limits, "recent_lead_chars": 200}
    if limits["index"] > 0:
        return {**limits, "index": limits["index"] // 2}
    if limits["changes"] > 0:
        return {**limits, "changes": limits["changes"] // 2}
    if limits["recent"] > 1:
        return {**limits, "recent": limits["recent"] - 1}
    if limits["pinned_chars"] > 300:
        return {**limits, "pinned_chars": 300}
    return None


def fit(scope: str, execution_id: str, parts: dict, budget: int) -> tuple[str, dict, int]:
    limits = {
        "index": len(parts["index"]),
        "changes": len(parts["changes"]),
        "recent": len(parts["recent"]),
        "recent_chars": 220,
        "recent_lead_chars": 900,
        "pinned_chars": 1500,
    }
    text = _render(scope, execution_id, parts, limits)
    reductions = 0
    while len(text) > budget:
        nxt = _reduce(limits, parts)
        if nxt is None:
            break
        limits = nxt
        text = _render(scope, execution_id, parts, limits)
        reductions += 1
    if len(text) > budget:
        # Header and pinned rules alone overflow: cut at a line boundary rather
        # than hand the host a block it will replace with a stub.
        cut = text[: budget - 2]
        text = cut[: cut.rfind("\n")] + "\n…" if "\n" in cut else cut + "…"
    return text, limits, reductions


def build_session_context(
    *,
    agent_id: str,
    user_id: str,
    readable_scopes: list[str],
    memory_scope: Optional[str] = None,
    cwd: Optional[str] = None,
    host_session_ref: Optional[str] = None,
    source: Optional[str] = None,
    budget: int = DEFAULT_BUDGET_CHARS,
    channel: str = "rest",
) -> dict:
    budget = max(MIN_BUDGET_CHARS, min(int(budget), MAX_BUDGET_CHARS))
    scope, resolved_by = resolve_scope(readable_scopes, memory_scope, cwd)
    if scope is None:
        return {
            "memory_scope": None,
            "resolved_by": resolved_by,
            "execution_id": None,
            "context": "",
        }

    execution = workspace_sync_service.start_session_execution(
        agent_id=agent_id,
        user_id=user_id,
        memory_scope=scope,
        host_session_ref=host_session_ref,
    )
    with get_db() as conn:
        pinned = _pinned(conn, scope)
        attention = _attention(conn, scope, agent_id)
        attention_ids = {a["id"] for a in attention}
        recent = _recent(conn, scope, attention_ids)
        needs_human = _needs_human(conn, scope)
    shown_activity_ids = attention_ids | {a["id"] for a in recent}
    changes = _changes(scope, agent_id, shown_activity_ids)
    # A record that just changed is already listed under the changes; the index
    # carries the rest rather than repeating it.
    changed_memory = {c["resource_id"] for c in changes if c["resource_type"] == "memory"}
    with get_db() as conn:
        index, index_total = _index(conn, scope, {p["id"] for p in pinned} | changed_memory)

    parts = {
        "agent_id": agent_id,
        "pinned": pinned,
        "needs_human": needs_human,
        "attention": attention,
        "recent": recent,
        "changes": changes,
        "index": index,
        "index_total": index_total,
    }
    text, limits, reductions = fit(scope, execution["execution_id"], parts, budget)

    injected_memory = (
        [p["id"] for p in pinned]
        + [c["resource_id"] for c in changes[: limits["changes"]] if c["resource_type"] == "memory"]
        + [r["id"] for r in index[: limits["index"]]]
    )
    injected_activity = [a["id"] for a in attention] + [a["id"] for a in recent[: limits["recent"]]]
    audit_service.write_event(
        actor_type="agent",
        actor_id=agent_id,
        action="context_injected",
        resource_type="execution",
        resource_id=execution["execution_id"],
        result="success",
        details={
            "memory_scope": scope,
            "resolved_by": resolved_by,
            "source": source,
            "channel": channel,
            "chars": len(text),
            "budget": budget,
            "reductions": reductions,
            "memory_ids": injected_memory,
            "activity_ids": injected_activity,
            "changes": min(len(changes), limits["changes"]),
            "changes_available": len(changes),
        },
    )
    return {
        "memory_scope": scope,
        "resolved_by": resolved_by,
        "execution_id": execution["execution_id"],
        "execution_reused": execution["reused"],
        "context": text,
        "chars": len(text),
        "reductions": reductions,
        "memory_ids": injected_memory,
        "activity_ids": injected_activity,
    }


HANDOFF_FIELDS = ("completed", "next_steps", "open_questions", "needs_human")
HANDOFF_MAX_CHARS = 4000


def normalize_handoff(value) -> dict:
    """Validate a structured handoff: what a next session needs to carry on.

    completed: one line on what got done. next_steps / open_questions: short
    lists. needs_human: what a person has to do or decide, if anything. Unknown
    keys are rejected so a typo does not silently drop the part that mattered.
    """
    if not isinstance(value, dict) or not value:
        raise ValueError("handoff must be a non-empty object")
    unknown = set(value) - set(HANDOFF_FIELDS)
    if unknown:
        raise ValueError(f"unknown handoff fields: {', '.join(sorted(unknown))}")
    handoff: dict = {}
    for key in ("completed", "needs_human"):
        if value.get(key) is not None:
            if not isinstance(value[key], str):
                raise ValueError(f"handoff.{key} must be a string")
            if value[key].strip():
                handoff[key] = value[key].strip()
    for key in ("next_steps", "open_questions"):
        items = value.get(key)
        if items is None:
            continue
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError(f"handoff.{key} must be a list of strings")
        cleaned = [i.strip() for i in items if i.strip()]
        if cleaned:
            handoff[key] = cleaned
    if not handoff:
        raise ValueError("handoff has no content")
    if len(json.dumps(handoff)) > HANDOFF_MAX_CHARS:
        raise ValueError(f"handoff is over {HANDOFF_MAX_CHARS} characters; keep it to what the next session needs")
    return handoff


def handoff_check(*, agent_id: str, user_id: str, host_session_ref: str) -> dict:
    """Should this session be asked, once, to record a handoff before it stops?

    Looks for an open activity of this agent that belongs to the host session:
    linked to one of its executions, or updated in the session's workspace since
    the session began (agents do not always pass execution_id). Asks at most once
    per activity: the request is stamped, and a stamped activity is never asked
    again, so a long conversation costs one extra turn per task at most.
    """
    from app.time_utils import utc_now_iso

    with get_db() as conn:
        executions = conn.execute(
            """SELECT id, memory_scope, started_at FROM agent_executions
            WHERE agent_id = ? AND user_id = ? AND host_session_ref = ? AND status = 'active'""",
            (agent_id, user_id, host_session_ref),
        ).fetchall()
        if not executions:
            return {"nudge": False}
        clauses, params = [], []
        for execution in executions:
            clauses.append("source_execution_id = ? OR (memory_scope = ? AND updated_at >= ?)")
            params += [execution["id"], execution["memory_scope"], execution["started_at"]]
        activity = conn.execute(
            f"""SELECT id, task_description, memory_scope FROM agent_activity
            WHERE COALESCE(assigned_agent_id, agent_id) = ? AND status = 'active'
              AND handoff_json IS NULL AND handoff_requested_at IS NULL
              AND ({' OR '.join(f'({c})' for c in clauses)})
            ORDER BY updated_at DESC LIMIT 1""",
            (agent_id, *params),
        ).fetchone()
        if not activity:
            return {"nudge": False}
        conn.execute(
            "UPDATE agent_activity SET handoff_requested_at = ? WHERE id = ?",
            (utc_now_iso(), activity["id"]),
        )
        conn.commit()

    audit_service.write_event(
        actor_type="agent",
        actor_id=agent_id,
        action="handoff_requested",
        resource_type="activity",
        resource_id=activity["id"],
        result="success",
        details={"memory_scope": activity["memory_scope"], "host_session_ref": host_session_ref},
    )
    reason = (
        f'{APP_NAME}: activity {activity["id"]} ("{_one_line(activity["task_description"], 120)}") '
        "is open with no handoff. If this task is finished or waiting on someone, record one now: "
        f'activity_update(memory_scope="{activity["memory_scope"]}", status="completed" or "blocked", '
        'task_result="...", handoff={"completed": "...", "next_steps": [...], '
        '"open_questions": [...], "needs_human": "what a person must do, if anything"}). '
        "If you are still mid-task, say so in one line and carry on. You will not be asked again for this task."
    )
    return {"nudge": True, "activity_id": activity["id"], "reason": reason}
