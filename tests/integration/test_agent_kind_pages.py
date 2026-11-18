"""An entry on the Agents page is an agent or a service, and the pages show the right ones.

An agent does work. A service only fetches the secrets it is given: one permission and no workspaces.
The names here are made up for the tests.
"""

import pytest

from app.database import get_db
from app.services import activity_service
from tests.conftest import operator_token_for


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def create_entry(client, token, entry_id, display_name, **extra):
    """Create one entry through the API and return it."""
    body = {"id": entry_id, "display_name": display_name}
    body.update(extra)
    r = client.post("/api/agents", headers=auth(token), json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["agent"]


def kinds_of(r):
    return {a["id"]: a["kind"] for a in r.json()["data"]["agents"]}


@pytest.fixture
def one_of_each(test_client, admin_token):
    """One agent that does work and one service that only fetches secrets."""
    agent = create_entry(test_client, admin_token, "worker-a", "Worker A", capabilities=["memory"])
    service = create_entry(test_client, admin_token, "batch-reader", "Batch Reader", kind="service")
    return agent, service


def test_the_list_carries_the_kind_of_every_entry(test_client, admin_token, one_of_each):
    agent, service = one_of_each
    r = test_client.get("/api/agents", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    entries = r.json()["data"]["agents"]
    assert {a["id"] for a in entries} >= {agent["id"], service["id"]}
    for entry in entries:
        assert entry["kind"] in ("agent", "service"), entry
    assert kinds_of(r)["worker-a"] == "agent"
    assert kinds_of(r)["batch-reader"] == "service"


def test_the_list_can_be_narrowed_to_one_kind(test_client, admin_token, one_of_each):
    services = test_client.get("/api/agents?kind=service", headers=auth(admin_token))
    assert services.status_code == 200, services.text
    assert {a["id"] for a in services.json()["data"]["agents"]} == {"batch-reader"}
    assert all(a["kind"] == "service" for a in services.json()["data"]["agents"])

    agents = test_client.get("/api/agents?kind=agent", headers=auth(admin_token))
    assert agents.status_code == 200, agents.text
    assert {a["id"] for a in agents.json()["data"]["agents"]} == {"worker-a"}
    assert all(a["kind"] == "agent" for a in agents.json()["data"]["agents"])


def test_an_unknown_kind_is_refused(test_client, admin_token):
    r = test_client.get("/api/agents?kind=bogus", headers=auth(admin_token))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_KIND"


def test_one_permission_and_no_workspace_makes_a_service(test_client, admin_token):
    entry = create_entry(test_client, admin_token, "fetcher", "Fetcher", capabilities=["credentials_resolve"])
    assert entry["kind"] == "service"
    assert entry["capabilities"] == ["credentials_resolve"]
    assert "workspace:" not in entry["read_scopes_json"]
    assert "workspace:" not in entry["write_scopes_json"]


def test_a_service_is_forced_to_hold_one_permission(test_client, admin_token):
    entry = create_entry(test_client, admin_token, "fetcher-b", "Fetcher B", kind="service")
    assert entry["kind"] == "service"
    assert entry["capabilities"] == ["credentials_resolve"]

    refused = test_client.post(
        "/api/agents",
        headers=auth(admin_token),
        json={"id": "fetcher-c", "display_name": "Fetcher C", "kind": "service", "capabilities": ["memory"]},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "SERVICE_RULE"
    assert test_client.get("/api/agents/fetcher-c", headers=auth(admin_token)).status_code == 404


def test_one_permission_on_an_agent_leaves_it_an_agent(test_client, admin_token):
    entry = create_entry(test_client, admin_token, "helper", "Helper", kind="agent", capabilities=["credentials_resolve"])
    assert entry["kind"] == "agent"
    assert entry["capabilities"] == ["credentials_resolve"]


def test_an_agent_with_recorded_work_cannot_become_a_service(test_client, admin_token):
    create_entry(test_client, admin_token, "busy", "Busy", capabilities=["memory"])
    activity_service.create_activity("busy", "admin", "A task it already did", "agent:busy")

    refused = test_client.put("/api/agents/busy", headers=auth(admin_token), json={"kind": "service"})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "SERVICE_RULE"

    unchanged = test_client.get("/api/agents/busy", headers=auth(admin_token)).json()["data"]["agent"]
    assert unchanged["kind"] == "agent"
    assert unchanged["capabilities"] == ["memory"]


def test_an_agent_with_nothing_recorded_can_become_a_service(test_client, admin_token):
    create_entry(test_client, admin_token, "quiet", "Quiet", capabilities=["memory"])
    r = test_client.put("/api/agents/quiet", headers=auth(admin_token), json={"kind": "service"})
    assert r.status_code == 200, r.text
    changed = test_client.get("/api/agents/quiet", headers=auth(admin_token)).json()["data"]["agent"]
    assert changed["kind"] == "service"
    assert changed["capabilities"] == ["credentials_resolve"]
    assert "workspace:" not in changed["read_scopes_json"]
    assert "workspace:" not in changed["write_scopes_json"]


def test_a_service_keeps_its_one_permission_until_it_is_changed(test_client, admin_token):
    create_entry(test_client, admin_token, "reader", "Reader", kind="service")

    refused = test_client.put("/api/agents/reader", headers=auth(admin_token), json={"capabilities": ["memory"]})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "SERVICE_RULE"

    changed = test_client.put("/api/agents/reader", headers=auth(admin_token), json={"kind": "agent"})
    assert changed.status_code == 200, changed.text
    now_allowed = test_client.put("/api/agents/reader", headers=auth(admin_token), json={"capabilities": ["memory"]})
    assert now_allowed.status_code == 200, now_allowed.text
    entry = test_client.get("/api/agents/reader", headers=auth(admin_token)).json()["data"]["agent"]
    assert entry["kind"] == "agent" and entry["capabilities"] == ["memory"]


def test_the_agents_page_lists_the_agents_and_the_services_apart(test_client, admin_token, one_of_each):
    page = test_client.get("/agents", headers=auth(admin_token))
    assert page.status_code == 200, page.text
    assert "Worker A" in page.text
    assert "Batch Reader" not in page.text
    # There is no switch between the two lists: the Agents page says where the services are.
    assert "agent-switch" not in page.text
    assert 'href="/services"' in page.text
    assert "Credentials &gt; Services" in page.text

    services = test_client.get("/services", headers=auth(admin_token))
    assert services.status_code == 200, services.text
    assert "Batch Reader" in services.text
    assert "Programs and containers that only fetch secrets." in services.text
    assert "agent-switch" not in services.text
    # The Connect page sets up an agent that does work; a service is set up with a key and a secret.
    assert "/integrations?agent_id=batch-reader" not in services.text


def test_the_agents_page_says_when_there_are_no_services(test_client, admin_token):
    page = test_client.get("/services", headers=auth(admin_token))
    assert page.status_code == 200, page.text
    assert "No services yet. Create one with the Service role." in page.text
    # The Agents page points at the list of services; it does not carry a count of its own.
    agents = test_client.get("/agents", headers=auth(admin_token))
    assert 'href="/services"' in agents.text
    assert "Services (0)" not in agents.text


def test_the_connect_page_offers_agents_only(test_client, admin_token, one_of_each):
    page = test_client.get("/integrations", headers=auth(admin_token))
    assert page.status_code == 200, page.text
    assert "Worker A" in page.text
    assert "Batch Reader" not in page.text
    assert 'value="batch-reader"' not in page.text


def test_the_workspace_member_picker_offers_agents_only(test_client, admin_token, one_of_each):
    page = test_client.get("/workspaces", headers=auth(admin_token))
    assert page.status_code == 200, page.text
    assert "Worker A" in page.text
    assert "Batch Reader" not in page.text
    assert "batch-reader" not in page.text


def test_another_person_sees_only_the_agents_of_their_own(test_client, admin_token, one_of_each):
    from app.services.auth_service import create_user

    create_user("alex", "alex@test.local", "testpassword123", "Alex", "user")
    alex = operator_token_for("alex")
    create_entry(test_client, alex, "worker-b", "Worker B", capabilities=["memory"])

    page = test_client.get("/agents", headers=auth(alex))
    assert page.status_code == 200, page.text
    assert "Worker B" in page.text
    assert "Batch Reader" not in page.text
    assert 'href="/services"' in page.text
    assert "Services (0)" not in page.text


def test_the_recorded_work_of_an_entry_is_what_keeps_it_an_agent(test_client, admin_token):
    """The rule the service kind is decided by: one permission and nothing of its own."""
    create_entry(test_client, admin_token, "still-an-agent", "Still An Agent", kind="agent")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_activity (id, agent_id, assigned_agent_id, user_id, task_description, status, "
            "memory_scope, started_at, heartbeat_at) VALUES ('t1', 'still-an-agent', 'still-an-agent', 'admin', "
            "'Work already done', 'active', 'agent:still-an-agent', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    refused = test_client.put("/api/agents/still-an-agent", headers=auth(admin_token), json={"kind": "service"})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "SERVICE_RULE"


def test_a_service_can_be_created_on_an_install_with_one_workspace(test_client, admin_token):
    """The one-workspace default that gives a new agent its workspace must not be applied to a service."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    only = test_client.post("/api/workspaces", headers=headers, json={"id": "only-one", "name": "Only one"})
    assert only.status_code in (200, 201), only.text
    for body in (
        {"id": "worker-svc-a", "display_name": "Worker svc A", "kind": "service", "capabilities": ["credentials_resolve"]},
        {"id": "worker-svc-b", "display_name": "Worker svc B", "capabilities": ["credentials_resolve"]},
    ):
        created = test_client.post("/api/agents", headers=headers, json=body)
        assert created.status_code == 201, created.text
        assert created.json()["data"]["agent"]["kind"] == "service"
        read = test_client.get(f"/api/agents/{body['id']}", headers=headers).json()["data"]["agent"]
        assert "workspace:only-one" not in read["read_scopes_json"]
