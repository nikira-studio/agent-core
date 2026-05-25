import pytest
import re

from app.branding import ENV_PREFIX
from app.services.auth_service import create_user, create_session, get_user_by_id
from app.services.agent_service import create_agent
from app.services.workspace_service import create_workspace
from app.services.credential_service import create_credential
from app.services import activity_service


@pytest.fixture
def setup_integrations_data(clean_db):
    create_user("brian", "brian@test.local", "password123", "Brian", "admin")
    session = create_session("brian", channel="dashboard")
    user = get_user_by_id("brian")

    create_workspace(
        "agent-core",
        owner_user_id="brian",
        name="Agent Core",
        description="Self-hosted agent infrastructure",
    )

    create_agent(
        agent_id="claude-code",
        display_name="Claude Code",
        owner_user_id="brian",
        read_scopes=["agent:claude-code", "workspace:agent-core", "user:brian"],
        write_scopes=["agent:claude-code", "workspace:agent-core"],
    )

    create_credential(
        scope="workspace:agent-core",
        name="github-token",
        value_plaintext="ghp_secret",
        label="GitHub Token",
        created_by="brian",
    )

    activity_service.create_activity(
        agent_id="claude-code",
        user_id="brian",
        task_description="Test task for Agent Core",
        memory_scope="workspace:agent-core",
    )

    return {"user_id": "brian", "user": user, "session_id": session["session_id"]}


@pytest.fixture
def integrations_client(test_client, setup_integrations_data):
    test_client.cookies.set("session_token", setup_integrations_data["session_id"])
    return test_client


def test_integrations_page_loads(integrations_client):
    r = integrations_client.get("/integrations")
    assert r.status_code == 200
    html = r.text
    assert "Integrations" in html
    assert "Generate setup instructions" in html
    assert "Current tool preset" in html
    assert 'href="/integrations?' in html
    assert 'id="user_id"' in html
    assert 'id="workspace_id"' in html
    assert 'id="agent_id"' in html
    assert 'id="target"' not in html
    assert 'id="output_type"' not in html
    assert "setup-tabs" in html


def test_integrations_shows_selectors(integrations_client):
    r = integrations_client.get("/integrations")
    assert r.status_code == 200
    html = r.text
    assert 'value="brian"' in html
    assert 'value="agent-core"' in html
    assert 'value="claude-code"' in html


def test_integrations_generates_claude_md(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    output_match = re.search(r"<pre class='output-block'>(.*?)</pre>", html, re.S)
    assert output_match is not None
    output = output_match.group(1)
    assert "CLAUDE.md" in html
    assert "open a fresh activity first" in output
    assert "task_note" in output
    assert "workspace:agent-core" in output
    assert "task_result" in output
    assert "user:brian" not in output
    assert "Agent ID:" not in output


def test_integrations_generates_env_vars(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert f"{ENV_PREFIX}URL" in html
    assert f"{ENV_PREFIX}API_KEY" in html
    assert f"{ENV_PREFIX}WORKSPACE_SCOPE" in html
    assert "workspace:agent-core" in html


def test_integrations_project_is_optional_for_env(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert "-- Optional --" in html
    assert f"{ENV_PREFIX}AGENT_ID" in html
    assert f"{ENV_PREFIX}USER_SCOPE" in html
    assert "workspace:your-workspace-id" in html


def test_integrations_generates_verification_prompt(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=generic_mcp&output_type=verification"
    )
    assert r.status_code == 200
    html = r.text
    assert "workspace:agent-core" in html
    assert "verification" in html.lower() or "Verify" in html
    assert "activity_update" in html
    assert "memory_get" in html
    assert "credential_list" in html
    assert "connectors_list" in html
    assert "connectors_bindings_list" in html
    assert "connectors_actions_list" in html
    assert "connectors_bindings_test" in html
    assert "connectors_summary" in html
    assert "task_result" in html


def test_integrations_access_checks_show_ok_for_good_agent(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "check-ok" in html
    assert "Agent active" in html
    assert "Workspace read/write access" in html
    assert "User preference read access" in html


def test_integrations_no_raw_credential_values_in_output(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "ghp_secret" not in html


def test_integrations_generate_connection_endpoint(integrations_client):
    r = integrations_client.post(
        "/api/integrations/generate-connection",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "output_type": "env"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["api_key"]
    assert f"{ENV_PREFIX}URL" in data["output"]


def test_integrations_preview_and_apply_access_endpoints(integrations_client):
    preview = integrations_client.post(
        "/api/integrations/preview",
        json={
            "user_id": "brian",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "target": "claude_code",
            "output_type": "claude_md",
        },
    )
    assert preview.status_code == 200
    preview_data = preview.json()["data"]
    assert "recommended_scopes" in preview_data
    assert "outputs" in preview_data

    apply_access = integrations_client.post(
        "/api/integrations/apply-access",
        json={
            "user_id": "brian",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "include_user_write": False,
        },
    )
    assert apply_access.status_code == 200
    assert "read_scopes" in apply_access.json()["data"]


def test_integrations_page_surfaces_artifact_validation(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=mcp_json"
    )
    assert r.status_code == 200
    html = r.text
    assert "Check Setup" not in html
    assert "Artifact validation:" not in html
