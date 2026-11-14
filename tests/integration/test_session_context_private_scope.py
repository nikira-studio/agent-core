"""A new agent with no workspace has a working session start from its own private scope."""

from app.database import get_db
from app.services import activity_service, agent_service, memory_service


def _agent(agent_id="solo", scopes=None):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    scopes = scopes or [f"agent:{agent_id}"]
    _, key = agent_service.create_agent(
        agent_id=agent_id, display_name=agent_id, owner_user_id=owner_id, read_scopes=scopes, write_scopes=scopes
    )
    return owner_id, {"Authorization": f"Bearer {key}"}


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def test_session_context_over_mcp_serves_the_agents_own_private_scope(test_client, admin_token):
    owner, headers = _agent()
    record, _ = memory_service.write_memory(
        content="The solo agent keeps its notes in the private memory.", memory_class="decision",
        scope="agent:solo", topic="notes",
    )
    task = activity_service.create_activity(agent_id="solo", user_id=owner, task_description="Left over work", memory_scope="agent:solo")
    r = _mcp(test_client, headers, "session_context", memory_scope="agent:solo", host_session_ref="s-1")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["memory_scope"] == "agent:solo" and data["execution_id"]
    assert task["id"] in data["context"] and record["id"] in data["context"]
    # The execution works for writes.
    ok = _mcp(test_client, headers, "activity_update", memory_scope="agent:solo", task_note="carrying on", execution_id=data["execution_id"])
    assert ok.status_code in (200, 201), ok.text


def test_with_no_scope_given_an_agent_without_a_workspace_gets_its_private_scope(test_client, admin_token):
    _, headers = _agent()
    r = _mcp(test_client, headers, "session_context")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["memory_scope"] == "agent:solo" and data["resolved_by"] == "private"


def test_it_works_over_rest_too(test_client, admin_token):
    _, headers = _agent()
    r = test_client.post("/api/session-context", headers=headers, json={"memory_scope": "agent:solo"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["memory_scope"] == "agent:solo"


def test_another_agents_private_scope_is_still_refused(test_client, admin_token):
    _agent("solo")
    _, headers = _agent("nosy")
    refused = _mcp(test_client, headers, "session_context", memory_scope="agent:solo")
    assert refused.status_code == 403
    rest = test_client.post("/api/session-context", headers=headers, json={"memory_scope": "agent:solo"})
    assert rest.status_code in (400, 403)
