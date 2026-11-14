import pytest


@pytest.fixture(autouse=True)
def _db(clean_db):
    pass


def _make_activity(agent_id, memory_scope):
    from app.services.activity_service import create_activity

    return create_activity(
        agent_id=agent_id,
        user_id="testuser",
        task_description="Do something",
        memory_scope=memory_scope,
    )














def test_update_activity_can_store_task_result(clean_db):
    from app.services.activity_service import create_activity, update_activity, get_activity

    act = create_activity(
        agent_id="agentA",
        user_id="testuser",
        task_description="Do something",
        memory_scope="agent:agentA",
    )
    update_activity(act["id"], status="completed", task_result="Finished the work")
    fresh = get_activity(act["id"])
    assert fresh["status"] == "completed"
    assert fresh["task_result"] == "Finished the work"


def test_update_activity_can_store_task_note(clean_db):
    from app.services.activity_service import create_activity, update_activity, get_activity

    act = create_activity(
        agent_id="agentA",
        user_id="testuser",
        task_description="Do something else",
        memory_scope="agent:agentA",
    )
    update_activity(act["id"], task_note="Worked through the next step")
    fresh = get_activity(act["id"])
    assert fresh["task_note"] == "Worked through the next step"








def test_attention_excludes_old_and_resolved_activities(clean_db):
    from app.database import get_db
    from app.services.activity_service import (
        create_activity,
        list_attention_activities,
        update_activity,
    )

    old = create_activity("agentA", "testuser", "Old blocked task")
    update_activity(old["id"], status="blocked")
    resolved = create_activity("agentA", "testuser", "Retry this task")
    update_activity(resolved["id"], status="blocked")
    retry = create_activity("agentA", "testuser", "Retry this task")
    update_activity(retry["id"], status="completed")
    current = create_activity("agentA", "testuser", "Current blocked task")
    update_activity(current["id"], status="blocked")

    with get_db() as conn:
        conn.execute(
            "UPDATE agent_activity SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (old["id"],),
        )
        conn.commit()

    attention = list_attention_activities(user_id="testuser", lookback_days=3)
    assert [activity["id"] for activity in attention] == [current["id"]]


def test_attention_keeps_unrelated_current_blocker(clean_db):
    from app.services.activity_service import (
        create_activity,
        list_attention_activities,
        update_activity,
    )

    blocked = create_activity("agentA", "testuser", "Needs a decision")
    update_activity(blocked["id"], status="blocked")
    completed = create_activity("agentA", "testuser", "Different completed task")
    update_activity(completed["id"], status="completed")

    attention = list_attention_activities(user_id="testuser", lookback_days=3)
    assert [activity["id"] for activity in attention] == [blocked["id"]]
