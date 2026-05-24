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


def test_claim_returns_matching_activity(clean_db):
    from app.services.activity_service import claim_next_activity

    _make_activity("agentA", "agent:agentA")
    result = claim_next_activity("agentA", ["agent:agentA", "workspace:proj"])
    assert result is not None
    assert result["assigned_agent_id"] == "agentA"
    assert result["task_description"] == "Do something"


def test_claim_rejects_wrong_scope(clean_db):
    from app.services.activity_service import claim_next_activity

    _make_activity("agentA", "workspace:proj-a")
    # agentA's task is in workspace:proj-a, but we only authorize workspace:proj-b
    result = claim_next_activity("agentA", ["workspace:proj-b", "agent:agentA"])
    assert result is None


def test_claim_returns_none_when_no_activities(clean_db):
    from app.services.activity_service import claim_next_activity

    result = claim_next_activity("agentA", ["agent:agentA"])
    assert result is None


def test_claim_returns_none_for_empty_scopes(clean_db):
    from app.services.activity_service import claim_next_activity

    _make_activity("agentA", "agent:agentA")
    result = claim_next_activity("agentA", [])
    assert result is None


def test_claim_does_not_return_completed_activity(clean_db):
    from app.services.activity_service import claim_next_activity, update_activity

    act = _make_activity("agentA", "agent:agentA")
    update_activity(act["id"], status="completed")
    result = claim_next_activity("agentA", ["agent:agentA"])
    assert result is None


def test_claim_does_not_return_cancelled_activity(clean_db):
    from app.services.activity_service import claim_next_activity, cancel_activity

    act = _make_activity("agentA", "agent:agentA")
    cancel_activity(act["id"])
    result = claim_next_activity("agentA", ["agent:agentA"])
    assert result is None


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


def test_claim_different_agent_cannot_claim(clean_db):
    from app.services.activity_service import claim_next_activity

    _make_activity("agentA", "agent:agentA")
    # agentB tries to claim agentA's task
    result = claim_next_activity("agentB", ["agent:agentA", "agent:agentB"])
    assert result is None


def test_claim_updates_heartbeat(clean_db):
    from app.services.activity_service import claim_next_activity, get_activity

    act = _make_activity("agentA", "agent:agentA")
    original_heartbeat = act["heartbeat_at"]
    import time; time.sleep(0.01)
    claimed = claim_next_activity("agentA", ["agent:agentA"])
    assert claimed is not None
    assert claimed["heartbeat_at"] >= original_heartbeat
    # Verify heartbeat persisted in DB
    fresh = get_activity(claimed["id"])
    assert fresh["heartbeat_at"] == claimed["heartbeat_at"]


def test_claim_returns_oldest_activity_first(clean_db):
    from app.services.activity_service import claim_next_activity
    import time

    act1 = _make_activity("agentA", "agent:agentA")
    time.sleep(0.01)
    _make_activity("agentA", "agent:agentA")
    claimed = claim_next_activity("agentA", ["agent:agentA"])
    assert claimed is not None
    assert claimed["id"] == act1["id"]
