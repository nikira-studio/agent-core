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


def test_integrations_route_redirects_to_agent_setup(admin_client):
    r = admin_client.get("/integrations", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/agent-setup"


def test_integrations_nav_has_single_setup_entry(admin_client):
    r = admin_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'href="/agent-setup"' in html
    assert "<span>Integration</span>" in html
    assert 'href="/integrations"' not in html
    assert "<span>Agent Setup</span>" not in html


def test_integrations_page_is_agent_setup_workflow(admin_client):
    r = admin_client.get("/agent-setup")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Integrations</h1>" in html
    assert "Generate setup instructions, environment variables, MCP config, and AI-facing prompts for connecting tools to Agent Core." in html
    assert 'action="/agent-setup#generated-output"' in html
    assert 'id="user_id"' in html
    assert 'id="workspace_id"' in html
    assert 'id="agent_id"' in html
    assert 'id="target"' not in html
    assert "setup-tabs" in html
    assert "Current tool preset:" in html
    assert "CLAUDE.md" in html
    assert "AGENTS.md" in html


def test_legacy_integrations_wizard_is_removed(admin_client):
    r = admin_client.get("/agent-setup")
    html = r.text
    assert 'id="step-1"' not in html
    assert 'id="step-2"' not in html
    assert 'id="step-3"' not in html
    assert "Choose Agent Identity" not in html
    assert "Create New Agent Identity" not in html
    assert "Generate Config" not in html
    assert "Existing Agent API Key" not in html


def test_mcp_manifest_is_valid_test_connection_target(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "s4agent", "display_name": "S4 Agent"},
    )
    assert r.status_code == 201
    key_response = test_client.post(
        "/api/agent-setup/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "s4agent", "output_type": "env"},
    )
    assert key_response.status_code == 200
    api_key = key_response.json()["data"]["api_key"]

    manifest = test_client.get("/mcp", headers={"Authorization": f"Bearer {api_key}"})
    assert manifest.status_code == 200
    assert manifest.json()["name"] == "Agent Core"
