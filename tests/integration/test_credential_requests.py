from app.database import get_db
from app.services import agent_service, workspace_service

SCOPE = "workspace:cred-req"


def _agent(admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("cred-req", "Cred Req", owner_id)
    _, api_key = agent_service.create_agent(
        agent_id="req-agent", display_name="Req Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {api_key}"}


def _request(client, headers, **params):
    body = {"scope": SCOPE, "name": "github-token", "purpose": "Push release tags", **params}
    return client.post("/mcp", headers=headers, json={"tool": "credential_request", "params": body})


def test_request_is_filled_by_a_person_and_the_agent_gets_only_the_reference(test_client, admin_token):
    agent = _agent(admin_token)
    admin = {"Authorization": f"Bearer {admin_token}"}

    first = _request(test_client, agent).json()["data"]
    assert first["status"] == "pending" and first["created"] is True
    again = _request(test_client, agent).json()["data"]
    assert again["request_id"] == first["request_id"] and again["created"] is False

    listed = test_client.get("/api/credentials/requests", headers=admin).json()["data"]["requests"]
    assert [r["id"] for r in listed] == [first["request_id"]]

    # An agent cannot answer its own request.
    denied = test_client.post(f"/api/credentials/requests/{first['request_id']}/fulfill",
                              headers=agent, json={"value": "x"})
    assert denied.status_code == 403

    filled = test_client.post(f"/api/credentials/requests/{first['request_id']}/fulfill",
                              headers=admin, json={"value": "ghp_secret"})
    assert filled.status_code == 200, filled.json()
    reference = filled.json()["data"]["entry"]["reference_name"]

    done = _request(test_client, agent).json()["data"]
    assert done == {"status": "fulfilled", "scope": SCOPE, "name": "github-token", "reference_name": reference}
    assert "ghp_secret" not in str(done)
    page = test_client.get("/credentials", headers=admin)
    assert "Requested by agents" not in page.text


def test_declined_request_is_not_reopened_by_asking_again(test_client, admin_token):
    agent = _agent(admin_token)
    admin = {"Authorization": f"Bearer {admin_token}"}
    request_id = _request(test_client, agent).json()["data"]["request_id"]
    assert test_client.post(f"/api/credentials/requests/{request_id}/cancel", headers=admin).status_code == 200
    again = _request(test_client, agent).json()["data"]
    assert again["status"] == "declined" and again["created"] is False


def test_request_needs_write_access_to_the_scope(test_client, admin_token):
    agent = _agent(admin_token)
    response = _request(test_client, agent, scope="workspace:not-mine")
    assert response.status_code == 403


def test_pending_requests_show_on_the_credentials_page(test_client, admin_token):
    agent = _agent(admin_token)
    _request(test_client, agent)
    page = test_client.get("/credentials", headers={"Authorization": f"Bearer {admin_token}"})
    assert "Requested by agents" in page.text and "Push release tags" in page.text


def test_credential_added_directly_closes_the_pending_request(test_client, admin_token):
    from app.services import credential_service

    agent = _agent(admin_token)
    admin = {"Authorization": f"Bearer {admin_token}"}
    _request(test_client, agent)
    credential_service.create_credential(scope=SCOPE, name="github-token", value_plaintext="v")
    assert _request(test_client, agent).json()["data"]["status"] == "fulfilled"
    assert test_client.get("/api/credentials/requests", headers=admin).json()["data"]["requests"] == []


def test_malformed_request_params_are_400_not_429(test_client, admin_token):
    agent = _agent(admin_token)
    assert _request(test_client, agent, name=["x"]).status_code == 400
    assert _request(test_client, agent, scope="workspace:Bad Scope!").status_code in (400, 403)


def test_a_moved_credential_is_not_requested_again_and_moving_it_back_closes_requests(test_client, admin_token):
    from app.services import credential_service

    agent = _agent(admin_token)
    admin = {"Authorization": f"Bearer {admin_token}"}
    request_id = _request(test_client, agent).json()["data"]["request_id"]
    entry = test_client.post(f"/api/credentials/requests/{request_id}/fulfill",
                             headers=admin, json={"value": "v"}).json()["data"]["entry"]

    # A person moves the filled credential elsewhere; the agent asking again
    # must not reopen a request for something that was answered.
    credential_service.move_credential(entry["id"], "agent:req-agent")
    again = _request(test_client, agent).json()["data"]
    assert again["created"] is False and "agent:req-agent" in again["message"]
    assert test_client.get("/api/credentials/requests", headers=admin).json()["data"]["requests"] == []


def test_listing_closes_a_request_answered_without_the_fill_button(test_client, admin_token):
    from app.services import credential_service

    agent = _agent(admin_token)
    admin = {"Authorization": f"Bearer {admin_token}"}
    _request(test_client, agent)
    # Added by hand (or moved in); the agent has not asked again yet.
    credential_service.create_credential(scope=SCOPE, name="github-token", value_plaintext="v")
    assert test_client.get("/api/credentials/requests", headers=admin).json()["data"]["requests"] == []
    page = test_client.get("/credentials", headers=admin)
    assert "Requested by agents" not in page.text
