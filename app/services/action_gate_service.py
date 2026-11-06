"""Which service actions wait for a person before they run.

A gate belongs to a service and an action (a connector type and its action),
never to one binding: an agent may create or edit bindings, so a switch on a
binding could be walked around. It names who it holds: every caller, or a list
of agents. Nothing is gated unless a person turns it on, and turning a gate off
never releases a request that is already waiting.
"""

from __future__ import annotations

import json
from typing import Optional

from app.database import get_db
from app.time_utils import utc_now_iso

ALL_CALLERS = "all"


def _parse(actors_json: str):
    try:
        value = json.loads(actors_json)
    except (TypeError, ValueError):
        return ALL_CALLERS
    if value == ALL_CALLERS:
        return ALL_CALLERS
    if isinstance(value, list):
        return sorted({str(item) for item in value if item})
    return ALL_CALLERS


def list_gates(connector_type_id: str) -> dict[str, object]:
    """{action: "all" or [agent ids]} for one service."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, actors_json FROM action_gates WHERE connector_type_id = ?",
            (connector_type_id,),
        ).fetchall()
    return {row["action"]: _parse(row["actors_json"]) for row in rows}


def gate_for(connector_type_id: str, action: str, actor_type: str, actor_id: str) -> Optional[object]:
    """The gate that holds this caller's run of this action, or None if it runs straight away."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT actors_json FROM action_gates WHERE connector_type_id = ? AND action = ?",
            (connector_type_id, action),
        ).fetchone()
    if row is None:
        return None
    actors = _parse(row["actors_json"])
    if actors == ALL_CALLERS:
        return actors
    # A named list holds those agents only; people using tokens or scripts are
    # held only by a gate that names every caller.
    return actors if actor_type == "agent" and actor_id in actors else None


def set_gate(
    connector_type_id: str,
    action: str,
    *,
    enabled: bool,
    actors,
    changed_by: str,
) -> dict:
    """Turn a gate on, change who it holds, or turn it off. Returns the before and after."""
    before = list_gates(connector_type_id).get(action)
    if not enabled:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM action_gates WHERE connector_type_id = ? AND action = ?",
                (connector_type_id, action),
            )
            conn.commit()
        return {"before": before, "after": None}
    if actors != ALL_CALLERS:
        actors = sorted({str(a) for a in (actors or []) if a})
        if not actors:
            raise ValueError("Pick at least one agent, or hold every caller")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO action_gates (connector_type_id, action, actors_json, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connector_type_id, action) DO UPDATE SET actors_json = excluded.actors_json",
            (connector_type_id, action, json.dumps(actors), changed_by, utc_now_iso()),
        )
        conn.commit()
    return {"before": before, "after": actors}
