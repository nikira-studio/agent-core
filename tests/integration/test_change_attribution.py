"""The workspace change feed names who really made a change, not the record's author."""

import json

from app.database import get_db
from app.services import activity_service, agent_service, memory_service, workspace_service

WS = "workspace:attr-a"


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("attr-a", "attr-a", owner_id)
    _, key = agent_service.create_agent(
        agent_id="attr-writer", display_name="Writer", owner_user_id=owner_id, read_scopes=[WS], write_scopes=[WS]
    )
    _, key2 = agent_service.create_agent(
        agent_id="attr-other", display_name="Other", owner_user_id=owner_id, read_scopes=[WS], write_scopes=[WS]
    )
    return owner_id, {"Authorization": f"Bearer {key}"}, {"Authorization": f"Bearer {key2}"}




def _changes(change_type=None):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT change_type, resource_id, source_agent_id FROM workspace_changes WHERE memory_scope = ? ORDER BY sequence",
            (WS,),
        ).fetchall()
    return [dict(r) for r in rows if change_type is None or r["change_type"] == change_type]


def _record(agent_id="attr-writer", content="A fact the writer recorded"):
    record = memory_service.write_memory(
        content=content, memory_class="fact", scope=WS,
        provenance_json=json.dumps({"agent_id": agent_id, "actor_id": agent_id}),
    )
    record = record[0] if isinstance(record, tuple) else record
    return record["id"]


def test_a_person_retracting_a_memory_is_named_not_its_author(test_client, admin_token):
    owner_id, writer, _ = _setup()
    rid = _record()
    r = test_client.post(f"/api/memory/retract?record_id={rid}", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    change = _changes("memory_retracted")[-1]
    assert change["resource_id"] == rid and change["source_agent_id"] == owner_id != "attr-writer"
    assert _changes("memory_written")[-1]["source_agent_id"] == "attr-writer"  # authorship is untouched


def test_restore_and_pin_are_attributed_to_the_person(test_client, admin_token):
    owner_id, _, _ = _setup()
    rid = _record()
    memory_service.retract_memory(rid, retracted_by="attr-other")
    assert _changes("memory_retracted")[-1]["source_agent_id"] == "attr-other"
    r = test_client.post(f"/api/memory/restore?record_id={rid}", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    assert _changes()[-1]["source_agent_id"] == owner_id


def test_superseding_names_the_agent_that_wrote_the_replacement(test_client, admin_token):
    _setup()
    old = _record("attr-writer", "Old statement of the fact")
    memory_service.write_memory(
        content="New statement of the fact", memory_class="fact", scope=WS, supersedes_id=old,
        provenance_json=json.dumps({"agent_id": "attr-other", "actor_id": "attr-other"}),
    )
    superseded = [c for c in _changes("memory_superseded") if c["resource_id"] == old][-1]
    assert superseded["source_agent_id"] == "attr-other"


def test_a_person_closing_a_blocked_task_is_named_not_the_agent(test_client, admin_token):
    owner_id, _, _ = _setup()
    task = activity_service.create_activity(agent_id="attr-writer", user_id=owner_id, task_description="T", memory_scope=WS)
    activity_service.update_activity(task["id"], status="blocked")
    assert _changes("activity_blocked")[-1]["source_agent_id"] == "attr-writer"
    r = test_client.put(
        f"/api/activity/{task['id']}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "completed", "task_result": "done by hand"},
    )
    assert r.status_code == 200, r.text
    assert _changes("activity_completed")[-1]["source_agent_id"] == owner_id


def test_the_agents_own_updates_still_name_the_agent(test_client, admin_token):
    owner_id, _, _ = _setup()
    task = activity_service.create_activity(agent_id="attr-writer", user_id=owner_id, task_description="T", memory_scope=WS)
    activity_service.update_activity(task["id"], task_note="progress", alive=True)
    assert _changes("activity_updated")[-1]["source_agent_id"] == "attr-writer"
