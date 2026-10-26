from app.database import get_db

from app.services import activity_service, agent_service, memory_service, workspace_service


def _agent(admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("sync-test", "Sync Test", owner_id)
    _, api_key = agent_service.create_agent(
        agent_id="sync-agent", display_name="Sync Agent", owner_user_id=owner_id,
        read_scopes=["workspace:sync-test"], write_scopes=["workspace:sync-test"],
    )
    return api_key


def test_two_executions_receive_and_acknowledge_workspace_changes(test_client, admin_token):
    api_key = _agent(admin_token)
    headers = {"Authorization": f"Bearer {api_key}"}

    first = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync", "params": {"memory_scope": "workspace:sync-test"}
    })
    second = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync", "params": {"memory_scope": "workspace:sync-test"}
    })
    assert first.status_code == second.status_code == 200
    execution_a = first.json()["data"]["execution_id"]
    execution_b = second.json()["data"]["execution_id"]
    assert execution_a != execution_b

    written = test_client.post("/mcp", headers=headers, json={
        "tool": "memory_write", "params": {
            "scope": "workspace:sync-test", "memory_class": "decision",
            "content": "Use stable sync change identifiers.", "topic": "sync-test",
            "execution_id": execution_a,
        }
    })
    assert written.status_code == 201, written.json()

    page = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync", "params": {
            "memory_scope": "workspace:sync-test", "execution_id": execution_b,
        }
    })
    assert page.status_code == 200, page.json()
    data = page.json()["data"]
    assert len(data["memory_changes"]) == 1
    assert data["other_session_changes"] == [data["memory_changes"][0]["id"]]
    assert data["memory_changes"][0]["source_execution_id"] == execution_a

    repeated = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync", "params": {
            "memory_scope": "workspace:sync-test", "execution_id": execution_b,
        }
    }).json()["data"]
    assert repeated["memory_changes"][0]["id"] == data["memory_changes"][0]["id"]

    ack = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync_ack", "params": {
            "memory_scope": "workspace:sync-test", "execution_id": execution_b,
            "cursor": data["next_cursor"],
        }
    })
    assert ack.status_code == 200, ack.json()
    empty = test_client.post("/mcp", headers=headers, json={
        "tool": "workspace_sync", "params": {
            "memory_scope": "workspace:sync-test", "execution_id": execution_b,
        }
    }).json()["data"]
    assert empty["memory_changes"] == []


def test_change_insert_is_atomic_with_memory_write(clean_db):
    from app.services import workspace_sync_service

    memory_service.write_memory("Atomic change", "fact", "workspace:atomic")
    with get_db() as conn:
        record = conn.execute("SELECT id FROM memory_records WHERE scope = 'workspace:atomic'").fetchone()
        change = conn.execute("SELECT * FROM workspace_changes WHERE memory_scope = 'workspace:atomic'").fetchone()
    assert record and change
    assert change["resource_id"] == record["id"]
    assert workspace_sync_service.run_maintenance()["workspace_changes_pruned"] == 0


def test_memory_lifecycle_changes_cover_all_trigger_branches(clean_db):
    original, error = memory_service.write_memory(
        "Original fact", "fact", "workspace:lifecycle", topic="lifecycle",
        subject_anchor="repo:app/schema.py",
    )
    assert error is None
    replacement, error = memory_service.write_memory(
        "Replacement fact", "fact", "workspace:lifecycle", topic="lifecycle",
        supersedes_id=original["id"], subject_anchor="repo:app/services/memory_service.py",
    )
    assert error is None

    assert memory_service.confirm_memory(
        replacement["id"], "Checked app/services/memory_service.py", "test-agent"
    )
    assert memory_service.set_subject_anchor(
        replacement["id"], "repo:app/schema.py", changed_by="test-agent"
    )
    assert memory_service.set_pinned(replacement["id"], True)

    retracted, error = memory_service.write_memory(
        "Temporary fact", "fact", "workspace:lifecycle", topic="temporary"
    )
    assert error is None
    assert memory_service.retract_memory(retracted["id"])

    with get_db() as conn:
        rows = conn.execute(
            "SELECT change_type, resource_id FROM workspace_changes "
            "WHERE memory_scope = 'workspace:lifecycle' ORDER BY sequence"
        ).fetchall()
    changes = [(row["change_type"], row["resource_id"]) for row in rows]

    assert ("memory_superseded", original["id"]) in changes
    assert ("memory_confirmed", replacement["id"]) in changes
    assert ("memory_reanchored", replacement["id"]) in changes
    assert ("memory_pin_changed", replacement["id"]) in changes
    assert ("memory_retracted", retracted["id"]) in changes


def test_unattended_reconfirm_does_not_emit_workspace_change(clean_db):
    record, error = memory_service.write_memory(
        "Verified fact", "fact", "workspace:quiet-confirm", topic="quiet",
        subject_anchor="repo:app/schema.py",
    )
    assert error is None

    def confirm_changes():
        with get_db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM workspace_changes WHERE memory_scope = "
                "'workspace:quiet-confirm' AND change_type = 'memory_confirmed'"
            ).fetchone()[0]

    assert memory_service.confirm_memory(
        record["id"], "anchor check: file exists", "verification-pass"
    )
    assert confirm_changes() == 0

    assert memory_service.confirm_memory(
        record["id"], "Read app/schema.py", "test-agent"
    )
    assert confirm_changes() == 1

    # A pin change on a verifier-confirmed record still reports the pin.
    assert memory_service.confirm_memory(
        record["id"], "anchor check: file exists", "verification-pass"
    )
    assert memory_service.set_pinned(record["id"], True)
    with get_db() as conn:
        last = conn.execute(
            "SELECT change_type FROM workspace_changes WHERE memory_scope = "
            "'workspace:quiet-confirm' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
    assert last == "memory_pin_changed"
    assert confirm_changes() == 1


def test_activity_lifecycle_changes_cover_trigger_branches(clean_db):
    first = activity_service.create_activity(
        agent_id="agent-a", user_id="user-a", task_description="First task",
        memory_scope="workspace:activity-lifecycle",
    )
    activity_service.heartbeat_activity(first["id"])
    assert activity_service.update_activity(first["id"], task_note="Progress")
    assert activity_service.reassign_activity(first["id"], "agent-b")
    assert activity_service.update_activity(first["id"], status="blocked")

    second = activity_service.create_activity(
        agent_id="agent-a", user_id="user-a", task_description="Second task",
        memory_scope="workspace:activity-lifecycle",
    )
    assert activity_service.update_activity(
        second["id"], status="completed", task_result="Done"
    )

    with get_db() as conn:
        rows = conn.execute(
            "SELECT change_type, resource_id FROM workspace_changes "
            "WHERE memory_scope = 'workspace:activity-lifecycle' ORDER BY sequence"
        ).fetchall()
    changes = [(row["change_type"], row["resource_id"]) for row in rows]

    assert changes.count(("activity_created", first["id"])) == 1
    assert changes.count(("activity_updated", first["id"])) == 1
    assert changes.count(("activity_reassigned", first["id"])) == 1
    assert changes.count(("activity_blocked", first["id"])) == 1
    assert changes.count(("activity_completed", second["id"])) == 1
