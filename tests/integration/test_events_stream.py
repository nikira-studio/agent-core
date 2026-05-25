"""
Integration tests for the SSE event stream.

The starlette TestClient runs the ASGI app synchronously and collects the full
response body before returning, so an infinite SSE generator cannot be tested
via the HTTP layer in the test suite. Instead:

 - auth enforcement is tested with a normal GET (returns 401 before the generator starts)
 - event publishing is tested by registering directly on the hub and verifying
   that activity/connector routes put events in the queue
"""




def test_sse_requires_auth(test_client):
    r = test_client.get("/api/events")
    assert r.status_code == 401


def test_activity_create_publishes_event(test_client, agent_token):
    from app.services.event_stream_service import event_hub

    client_id, q = event_hub.register()
    try:
        r = test_client.post(
            "/api/activity",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"task_description": "hub publish test"},
        )
        assert r.status_code == 201
        activity_id = r.json()["data"]["activity"]["id"]

        payload = q.get_nowait()
        assert payload["type"] == "activity_created"
        assert payload["data"]["activity_id"] == activity_id
    finally:
        event_hub.unregister(client_id)


def test_activity_update_publishes_event(test_client, agent_token):
    from app.services.event_stream_service import event_hub

    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "update event test"},
    )
    assert r.status_code == 201
    activity_id = r.json()["data"]["activity"]["id"]

    client_id, q = event_hub.register()
    try:
        r2 = test_client.put(
            f"/api/activity/{activity_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "task_note": "Event stream progress note",
                "status": "completed",
                "task_result": "Event stream result test",
            },
        )
        assert r2.status_code == 200

        payload = q.get_nowait()
        assert payload["type"] == "activity_updated"
        assert payload["data"]["activity_id"] == activity_id
        assert payload["data"]["task_note"] == "Event stream progress note"
        assert payload["data"]["task_result"] == "Event stream result test"
    finally:
        event_hub.unregister(client_id)


def test_activity_cancel_publishes_event(test_client, agent_token):
    from app.services.event_stream_service import event_hub

    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "cancel event test"},
    )
    assert r.status_code == 201
    activity_id = r.json()["data"]["activity"]["id"]

    client_id, q = event_hub.register()
    try:
        r2 = test_client.delete(
            f"/api/activity/{activity_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert r2.status_code == 200

        payload = q.get_nowait()
        assert payload["type"] == "activity_cancelled"
        assert payload["data"]["activity_id"] == activity_id
    finally:
        event_hub.unregister(client_id)


def test_event_payload_has_required_fields(test_client, agent_token):
    from app.services.event_stream_service import event_hub

    client_id, q = event_hub.register()
    try:
        test_client.post(
            "/api/activity",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"task_description": "payload shape test"},
        )
        payload = q.get_nowait()
        assert "type" in payload
        assert "timestamp" in payload
        assert "data" in payload
    finally:
        event_hub.unregister(client_id)


def test_hub_subscriber_cleanup_on_unregister():
    from app.services.event_stream_service import event_hub

    before = event_hub.subscriber_count
    client_id, _ = event_hub.register()
    assert event_hub.subscriber_count == before + 1
    event_hub.unregister(client_id)
    assert event_hub.subscriber_count == before
