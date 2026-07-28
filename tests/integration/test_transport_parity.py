"""What an agent does over MCP has to look the same as over REST.

The two transports each reimplemented the announcement step. REST published to
the dashboard's event stream and dispatched webhooks; MCP dispatched webhooks
and nothing else, so an agent working over MCP — the path agents actually use —
never moved the live dashboard. Nobody noticed because both paths "worked".
"""

import pytest

from app.services import activity_service


@pytest.fixture
def captured(monkeypatch):
    """Record what each transport announced, without any listener attached."""
    events = {"stream": [], "webhook": []}

    from app.services import webhook_service
    from app.services.event_stream_service import event_hub

    monkeypatch.setattr(
        event_hub, "publish", lambda name, data: events["stream"].append(name)
    )
    monkeypatch.setattr(
        webhook_service,
        "dispatch_event",
        lambda name, data: events["webhook"].append(name),
    )
    return events


def _mcp(client, token, **params):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "activity_update", "params": params},
    )
    assert r.status_code in (200, 201), r.json()
    return r.json()["data"]["activity"]


def test_opening_work_over_mcp_reaches_the_dashboard(test_client, agent_token, captured):
    _mcp(test_client, agent_token, task_description="Investigating the failure")

    assert captured["stream"] == ["activity_created"], (
        "the live dashboard never heard about work started over MCP"
    )
    assert captured["webhook"] == ["activity_created"]


def test_a_heartbeat_over_mcp_reaches_the_dashboard(test_client, agent_token, captured):
    _mcp(test_client, agent_token, task_description="Working")
    captured["stream"].clear()
    captured["webhook"].clear()

    _mcp(test_client, agent_token)

    assert captured["stream"] == ["activity_heartbeat"]
    assert captured["webhook"] == ["activity_heartbeat"]


def test_closing_work_over_mcp_reaches_the_dashboard(test_client, agent_token, captured):
    _mcp(test_client, agent_token, task_description="Working")
    captured["stream"].clear()

    _mcp(test_client, agent_token, status="completed", task_result="done")

    assert captured["stream"] == ["activity_updated"]


def test_cancelling_over_mcp_announces_a_cancellation(test_client, agent_token, captured):
    _mcp(test_client, agent_token, task_description="Working")
    captured["stream"].clear()

    _mcp(test_client, agent_token, status="cancelled")

    assert captured["stream"] == ["activity_cancelled"]


def test_both_transports_announce_the_same_way(test_client, agent_token, captured):
    """REST and MCP creating work must produce the same two announcements."""
    rest = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "task_description": "From REST",
            "memory_scope": "agent:testagent",
        },
    )
    assert rest.status_code in (200, 201), rest.json()
    rest_events = (list(captured["stream"]), list(captured["webhook"]))

    # Close it first: an agent has one open activity at a time, so leaving this
    # one open would make the MCP call an update and compare two different things.
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"status": "completed", "task_result": "done"},
        },
    )

    captured["stream"].clear()
    captured["webhook"].clear()
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "From MCP"},
        },
    )
    mcp_events = (list(captured["stream"]), list(captured["webhook"]))

    assert rest_events == mcp_events


def test_delivery_lives_in_one_place(test_client):
    """Both routers call the service, so there is nothing left to drift."""
    import pathlib

    assert callable(activity_service.notify)
    for router in ("app/routes/activity.py", "app/routes/mcp.py"):
        source = pathlib.Path(router).read_text()
        assert "event_hub.publish(" not in source, f"{router} announces directly again"
        assert "webhook_service.dispatch_event(\"activity" not in source, (
            f"{router} dispatches activity webhooks directly again"
        )
