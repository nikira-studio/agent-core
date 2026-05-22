import pytest


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


def test_integrations_page_requires_auth(test_client, clean_db):
    r = test_client.get("/integrations", follow_redirects=False)
    assert r.status_code == 302


def test_integrations_route_is_current_page(admin_client):
    r = admin_client.get("/integrations", follow_redirects=False)
    assert r.status_code == 200
    assert "<h1>Integrations</h1>" in r.text


def test_integrations_nav_has_single_setup_entry(admin_client):
    r = admin_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'href="/integrations"' in html
    assert "<span>Integrations</span>" in html


def test_integrations_page_is_current_workflow(admin_client):
    r = admin_client.get("/integrations")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Integrations</h1>" in html
    assert "Generate setup instructions, environment variables, MCP config, and AI-facing prompts for connecting tools to Agent Core." in html
    assert 'action="/integrations#generated-output"' in html
    assert 'id="user_id"' in html
    assert 'id="workspace_id"' in html
    assert 'id="agent_id"' in html
    assert 'id="target"' not in html
    assert "setup-tabs" in html
    assert "Current tool preset:" in html
    assert "CLAUDE.md" in html
    assert "AGENTS.md" in html


def test_overview_page_has_operational_search(admin_client):
    r = admin_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Operational Search" in html
    assert 'id="dashboard-search-query"' in html
    assert "Search memory, activities, briefings, connector types, and visible bindings." in html


def test_legacy_integrations_wizard_is_removed(admin_client):
    r = admin_client.get("/integrations")
    html = r.text
    assert 'id="step-1"' not in html
    assert 'id="step-2"' not in html
    assert 'id="step-3"' not in html
    assert "Choose Agent Identity" not in html
    assert "Create New Agent Identity" not in html
    assert "Generate Config" not in html
    assert "Existing Agent API Key" not in html
    assert "Setup Verification" not in html


def test_mcp_manifest_is_valid_test_connection_target(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "s4agent", "display_name": "S4 Agent"},
    )
    assert r.status_code == 201
    key_response = test_client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "s4agent", "output_type": "env"},
    )
    assert key_response.status_code == 200
    api_key = key_response.json()["data"]["api_key"]

    manifest = test_client.get("/mcp", headers={"Authorization": f"Bearer {api_key}"})
    assert manifest.status_code == 200
    assert manifest.json()["name"] == "Agent Core"


def test_dashboard_search_spans_operational_state(admin_client):
    import json

    from app.database import get_db
    from app.services import activity_service, briefing_service, connector_service, memory_service

    activity = activity_service.create_activity(
        "search-alpha-agent",
        "admin",
        "Search Alpha task",
        memory_scope="user:admin",
    )
    memory_service.write_memory(
        content="Search Alpha briefing needle",
        memory_class="fact",
        scope="user:admin",
        source_kind="agent_inference",
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_types
            (id, display_name, description, auth_type, supported_actions_json,
             required_credential_fields_json, default_binding_rules_json, disabled_actions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "search-alpha-connector",
                "Search Alpha Connector",
                "Search Alpha connector used for dashboard search",
                "bearer",
                json.dumps(["search_alpha"]),
                json.dumps([]),
                None,
                json.dumps([]),
            ),
        )
        conn.commit()
    connector_service.create_binding(
        "search-alpha-connector",
        "Search Alpha Binding",
        "shared",
        enabled=True,
    )
    briefing = briefing_service.generate_handoff_briefing(
        activity["id"],
        requesting_agent_id="search-alpha-agent",
        requesting_user_id="admin",
    )
    assert briefing

    r = admin_client.post(
        "/api/dashboard/search",
        json={"query": "Search Alpha", "limit": 5},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["counts"]["memory"] >= 1
    assert data["counts"]["activities"] >= 1
    assert data["counts"]["briefings"] >= 1
    assert data["counts"]["connector_types"] >= 1
    assert data["counts"]["bindings"] >= 1
