import pytest
import json
import re
from app.services.auth_service import create_user, create_session, get_user_by_id
from app.services.agent_service import create_agent, get_agent_by_id
from app.services.workspace_service import create_workspace
from app.services.vault_service import create_vault_entry
from app.services import activity_service


@pytest.fixture
def setup_agent_setup_data(clean_db):
    create_user("brian", "brian@test.local", "password123", "Brian", "admin")
    session = create_session("brian", channel="dashboard")
    user = get_user_by_id("brian")

    create_workspace("agent-core", owner_user_id="brian", name="Agent Core", description="Self-hosted agent infrastructure")

    create_agent(
        agent_id="claude-code",
        display_name="Claude Code",
        owner_user_id="brian",
        read_scopes=["agent:claude-code", "workspace:agent-core", "user:brian"],
        write_scopes=["agent:claude-code", "workspace:agent-core"],
    )

    create_vault_entry(
        scope="workspace:agent-core",
        name="github-token",
        value_plaintext="ghp_secret",
        label="GitHub Token",
        value_type="api",
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
def agent_setup_client(test_client, setup_agent_setup_data):
    test_client.cookies.set("session_token", setup_agent_setup_data["session_id"])
    return test_client


def test_agent_setup_page_loads(agent_setup_client):
    r = agent_setup_client.get("/agent-setup")
    assert r.status_code == 200
    html = r.text
    assert "Integrations" in html
    assert "Generate setup instructions" in html
    assert "Current tool preset" in html
    assert "First-class presets are Claude Code, Codex, Cursor, Windsurf, and Generic MCP/REST" in html
    assert 'href="/agent-setup"' in html
    assert 'id="user_id"' in html
    assert 'id="workspace_id"' in html
    assert 'id="agent_id"' in html
    assert 'id="target"' not in html
    assert 'id="output_type"' not in html
    assert "setup-tabs" in html


def test_agent_setup_shows_selectors(agent_setup_client):
    r = agent_setup_client.get("/agent-setup")
    assert r.status_code == 200
    html = r.text
    assert 'value="brian"' in html
    assert 'value="agent-core"' in html
    assert 'value="claude-code"' in html


def test_agent_setup_generates_instructions_output(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "workspace:agent-core" in html
    assert "user:brian" in html
    assert "agent:claude-code" in html
    assert "Instructions" in html
    assert "What To Generate" in html
    assert "Generate One-Time Key" in html


def test_agent_setup_generates_claude_md(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    output_match = re.search(r"<pre class='output-block'>(.*?)</pre>", html, re.S)
    assert output_match is not None
    output = output_match.group(1)
    assert "CLAUDE.md" in html
    assert "workspace:agent-core" in output
    assert "user:brian" not in output
    assert "Brian" not in output
    assert "active Agent Core user and agent identities are determined by the MCP/API key" in html
    assert "Agent ID:" not in html
    assert "ghp_" not in html


def test_agent_setup_generates_agents_md(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=codex&output_type=agents_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "AGENTS.md" in html
    assert "workspace:agent-core" in html
    assert "user:brian" not in html
    assert "active Agent Core user and agent identities are determined by the MCP/API key" in html
    assert "Agent ID:" not in html
    assert "ghp_" not in html


def test_agent_setup_generates_mcp_json(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=cursor&output_type=mcp_json"
    )
    assert r.status_code == 200
    html = r.text
    assert "mcpServers" in html
    assert "agent-core" in html
    assert "/mcp" in html
    assert "AGENT_CORE_API_KEY" in html


def test_agent_setup_generates_env_vars(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert "AGENT_CORE_URL" in html
    assert "AGENT_CORE_API_KEY" in html
    assert "AGENT_CORE_WORKSPACE_SCOPE" in html
    assert "AGENT_CORE_MEMORY_SCOPE" not in html
    assert "workspace:agent-core" in html


def test_agent_setup_project_is_optional_for_env(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert "-- Optional --" in html
    assert "AGENT_CORE_AGENT_ID" in html
    assert "AGENT_CORE_USER_SCOPE" in html
    assert "workspace:your-workspace-id" in html
    assert "Select a user, workspace, and agent" not in html


def test_agent_setup_generates_session_prompt(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=session"
    )
    assert r.status_code == 200
    html = r.text
    assert "Session Prompt" in html
    assert "You are Claude Code" in html
    assert "Default memory scope" in html
    assert "workspace:agent-core" in html


def test_agent_setup_generates_verification_prompt(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=generic_mcp&output_type=verification"
    )
    assert r.status_code == 200
    html = r.text
    assert "workspace:agent-core" in html
    assert "verification" in html.lower() or "Verify" in html


def test_agent_setup_access_checks_show_ok_for_good_agent(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "check-ok" in html
    assert "Agent active" in html
    assert "Workspace read/write access" in html
    assert "User preference read access" in html


def test_agent_setup_no_raw_vault_values_in_output(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "ghp_secret" not in html
    assert "ghp_" not in html


def test_agent_setup_copy_button_present_when_output_generated(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "output-block" in html or "Copy" in html
    assert "Download" in html
    assert "Regenerate" in html


def test_agent_setup_output_tabs_present(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=cursor&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "setup-tabs" in html
    assert "Instructions" in html
    assert "MCP Config" in html
    assert "Session Prompt" in html
    assert "CLAUDE.md" in html
    assert "AGENTS.md" in html
    assert "Verification Prompt" in html
    assert "Cursor MCP" not in html


def test_agent_setup_page_always_includes_full_guidance(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "Do not use user preference memory" not in html
    assert "Skip activity tracking" not in html
    assert "vault_get" in html


def test_agent_setup_guidance_options_are_not_user_controls(agent_setup_client):
    r = agent_setup_client.get("/agent-setup")
    assert r.status_code == 200
    html = r.text
    assert "Add user preference guidance" not in html
    assert "Add vault credential guidance" not in html
    assert "Add activity and handoff guidance" not in html
    assert "These options only change the generated output below" not in html
    assert 'id="generated-output"' in html
    assert 'action="/agent-setup#generated-output"' in html
    assert "function submitAgentSetupToOutput(input)" in html


def test_agent_setup_destination_guidance_for_claude_md(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "CLAUDE.md" in html
    assert "workspace repository root" in html


def test_agent_setup_destination_guidance_for_env(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert "shell profile" in html or ".bashrc" in html or ".zshrc" in html


def test_agent_setup_requires_auth(test_client):
    test_client.cookies.clear()
    r = test_client.get("/agent-setup", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_agent_setup_access_check_info_scope_warning(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "global" in html.lower() or "scope" in html.lower()


def test_agent_setup_next_steps_section(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "Next Steps" in html
    assert "verification" in html.lower() or "verify" in html.lower()


def test_agent_setup_uses_request_base_url(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=env",
        headers={"host": "core.example.test"},
    )
    assert r.status_code == 200
    html = r.text
    assert 'AGENT_CORE_URL=&quot;http://core.example.test&quot;' in html
    assert "localhost" not in html


def test_agent_setup_uses_safe_api_key_placeholder(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=cursor&output_type=mcp_json"
    )
    assert r.status_code == 200
    html = r.text
    assert "{{AGENT_CORE_API_KEY}}" in html
    assert "ac_sk_" not in html


def test_agent_setup_generate_connection_rotates_key_and_injects_output(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/generate-connection",
        json={
            "user_id": "brian",
            "agent_id": "claude-code",
            "target": "claude_code",
            "output_type": "env",
        },
    )
    assert r.status_code == 200, r.json()
    data = r.json()["data"]
    assert data["api_key"].startswith("ac_sk_")
    assert f'AGENT_CORE_API_KEY="{data["api_key"]}"' in data["output"]
    assert 'AGENT_CORE_AGENT_ID="claude-code"' in data["output"]
    assert "workspace:your-workspace-id" in data["output"]
    assert "shown once" in data["warning"]


def test_agent_setup_mcp_config_contains_codex_and_generic_sections(agent_setup_client):
    from app.routes.dashboard import _build_mcp_json

    output = _build_mcp_json("http://core.test")
    assert "# Codex CLI: add this to ~/.codex/config.toml" in output
    assert "[mcp_servers.agent-core]" in output
    assert 'url = "http://core.test/mcp"' in output
    assert 'http_headers = { Authorization = "Bearer {{AGENT_CORE_API_KEY}}" }' in output
    assert "# OpenCode: add this under ~/.config/opencode/opencode.json" in output
    assert '"type": "remote"' in output
    assert '"enabled": true' in output
    assert '"mcpServers"' in output
    assert '"Authorization": "Bearer {{AGENT_CORE_API_KEY}}"' in output


def test_agent_setup_generate_connection_uses_same_codex_header_shape(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/generate-connection",
        json={
            "user_id": "brian",
            "agent_id": "claude-code",
            "target": "codex",
            "output_type": "mcp_json",
        },
    )
    assert r.status_code == 200, r.json()
    data = r.json()["data"]
    assert data["api_key"].startswith("ac_sk_")
    assert 'http_headers = { Authorization = "Bearer ' in data["output"]
    assert 'bearer_token_env_var' not in data["output"]
    assert data["api_key"] in data["output"]


def test_agent_setup_preview_endpoint_returns_outputs_and_recommended_scopes(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/preview",
        json={
            "user_id": "brian",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "target": "generic_mcp",
            "output_type": "mcp_json",
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "mcp_json" in data["outputs"]
    assert "claude_md" in data["outputs"]
    assert "agents_md" in data["outputs"]
    assert "workspace:agent-core" in data["recommended_scopes"]["read"]
    assert data["access_checks"]
    assert "{{AGENT_CORE_API_KEY}}" in data["selected_output"]


def test_agent_setup_preview_endpoint_always_includes_full_guidance(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/preview",
        json={
            "user_id": "brian",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "target": "claude_code",
            "output_type": "claude_md",
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "Do not use user preference memory" not in data["selected_output"]
    assert "vault_get" in data["selected_output"]
    assert "Activity Tracking" in data["selected_output"]


def test_agent_setup_non_admin_cannot_generate_for_unowned_agent(test_client, clean_db):
    create_user("brian", "brian@test.local", "password123", "Brian", "user")
    create_user("other", "other@test.local", "password123", "Other", "user")
    session = create_session("brian", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    create_workspace("agent-core", owner_user_id="brian", name="Agent Core")
    create_agent(
        agent_id="other-agent",
        display_name="Other Agent",
        owner_user_id="other",
        read_scopes=["agent:other-agent", "workspace:agent-core", "user:brian"],
        write_scopes=["agent:other-agent", "workspace:agent-core"],
    )

    r = test_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=other-agent&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "Other Agent" not in html
    assert "Agent not found" in html
    assert "Agent Core Context" not in html


def test_agent_setup_missing_project_access_produces_warning(agent_setup_client):
    create_agent(
        agent_id="limited-agent",
        display_name="Limited Agent",
        owner_user_id="brian",
        read_scopes=["agent:limited-agent"],
        write_scopes=["agent:limited-agent"],
    )
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=limited-agent&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "No workspace access" in html
    assert "Recommended: add workspace scope to agent" in html
    assert "check-blocked" in html or "check-warn" in html


def test_agent_setup_phase_1_does_not_mutate_agent_scopes(agent_setup_client):
    before = get_agent_by_id("claude-code")
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    after = get_agent_by_id("claude-code")
    assert after["read_scopes_json"] == before["read_scopes_json"]
    assert after["write_scopes_json"] == before["write_scopes_json"]


def test_apply_access_updates_agent_scopes(agent_setup_client):
    from app.services.agent_service import parse_scopes

    before = get_agent_by_id("claude-code")
    before_read = parse_scopes(before["read_scopes_json"])
    before_write = parse_scopes(before["write_scopes_json"])
    assert "workspace:agent-core" not in before_read or "workspace:agent-core" in before_write

    r = agent_setup_client.post(
        "/agent-setup/apply-access",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "include_user_write": False}
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True

    after = get_agent_by_id("claude-code")
    after_read = parse_scopes(after["read_scopes_json"])
    after_write = parse_scopes(after["write_scopes_json"])
    assert "workspace:agent-core" in after_read
    assert "workspace:agent-core" in after_write
    assert "agent:claude-code" in after_read
    assert "agent:claude-code" in after_write
    assert "user:brian" in after_read


def test_apply_access_planned_api_route_updates_agent_scopes(agent_setup_client):
    from app.services.agent_service import parse_scopes

    r = agent_setup_client.post(
        "/api/agent-setup/apply-recommended-access",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "include_user_write": False}
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True

    after = get_agent_by_id("claude-code")
    after_read = parse_scopes(after["read_scopes_json"])
    after_write = parse_scopes(after["write_scopes_json"])
    assert "workspace:agent-core" in after_read
    assert "workspace:agent-core" in after_write


def test_apply_access_with_user_write_includes_user_in_write_scopes(agent_setup_client):
    from app.services.agent_service import parse_scopes

    r = agent_setup_client.post(
        "/agent-setup/apply-access",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "include_user_write": True}
    )
    assert r.status_code == 200

    after = get_agent_by_id("claude-code")
    after_write = parse_scopes(after["write_scopes_json"])
    assert "user:brian" in after_write


def test_apply_access_writes_audit_event(agent_setup_client):
    from app.services import audit_service

    r = agent_setup_client.post(
        "/agent-setup/apply-access",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "include_user_write": False}
    )
    assert r.status_code == 200

    events = audit_service.query_events(action="scope_grant", limit=1)
    assert len(events) >= 1
    assert events[0]["resource_id"] == "claude-code"


def test_apply_access_non_admin_cannot_apply_for_other_user(agent_setup_client, clean_db):
    create_user("other", "other@test.local", "password123", "Other", "user")
    other_session = create_session("other", channel="dashboard")
    agent_setup_client.cookies.set("session_token", other_session["session_id"])

    r = agent_setup_client.post(
        "/agent-setup/apply-access",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "include_user_write": False}
    )
    assert r.status_code == 403


def test_apply_access_non_admin_cannot_apply_unowned_project(agent_setup_client):
    create_user("owner", "owner@test.local", "password123", "Owner", "user")
    create_user("other", "other@test.local", "password123", "Other", "user")
    create_workspace("owner-workspace", owner_user_id="owner", name="Owner Workspace")
    create_workspace("other-workspace", owner_user_id="other", name="Other Workspace")
    create_agent(
        agent_id="owner-agent",
        display_name="Owner Agent",
        owner_user_id="owner",
        read_scopes=["agent:owner-agent"],
        write_scopes=["agent:owner-agent"],
    )
    owner_session = create_session("owner", channel="dashboard")
    agent_setup_client.cookies.set("session_token", owner_session["session_id"])

    r = agent_setup_client.post(
        "/api/agent-setup/apply-recommended-access",
        json={"user_id": "owner", "workspace_id": "other-workspace", "agent_id": "owner-agent", "include_user_write": False}
    )
    assert r.status_code == 403


def test_apply_access_rejects_missing_project(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/apply-recommended-access",
        json={"user_id": "brian", "workspace_id": "missing-workspace", "agent_id": "claude-code", "include_user_write": False}
    )
    assert r.status_code == 404


def test_verify_endpoint_reports_mcp_connectivity(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["data"]["ok"] is True
    checks = {c["check"]: c for c in data["data"]["checks"]}
    assert checks["api_connectivity"]["status"] == "ok"
    assert checks["mcp_connectivity"]["status"] == "ok"


def test_verify_endpoint_writes_test_memory_to_project(agent_setup_client):
    from app.database import get_db

    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code", "write_test_memory": True},
    )
    assert r.status_code == 200
    data = r.json()
    assert "checks" in data["data"]
    checks = {c["check"]: c for c in data["data"]["checks"]}
    assert checks["memory_write"]["status"] == "ok"
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT content, memory_class, scope, domain, topic, source_kind
            FROM memory_records
            WHERE id = ?
            """,
            (checks["memory_write"]["record_id"],),
        ).fetchone()
    assert row is not None
    assert row["scope"] == "workspace:agent-core"
    assert row["memory_class"] == "fact"
    assert row["domain"] == "setup"
    assert row["topic"] == "verification"
    assert row["source_kind"] == "tool_output"
    assert "setup verification" in row["content"]


def test_verify_endpoint_skips_test_memory_by_default(agent_setup_client):
    from app.database import get_db

    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code"},
    )
    assert r.status_code == 200
    checks = {c["check"]: c for c in r.json()["data"]["checks"]}
    assert checks["memory_write"]["status"] == "skipped"
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM memory_records WHERE scope = ? AND domain = ? AND topic = ?",
            ("workspace:agent-core", "setup", "verification"),
        ).fetchone()
    assert row["count"] == 0


def test_verify_endpoint_non_admin_cannot_verify_other_user(agent_setup_client):
    create_user("other-verify", "other-verify@test.local", "password123", "Other Verify", "user")
    other_session = create_session("other-verify", channel="dashboard")
    agent_setup_client.cookies.set("session_token", other_session["session_id"])
    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "claude-code"},
    )
    assert r.status_code == 403


def test_verify_endpoint_non_admin_cannot_verify_unowned_project(agent_setup_client):
    create_user("owner-verify", "owner-verify@test.local", "password123", "Owner Verify", "user")
    create_user("other-workspace-owner", "other-workspace-owner@test.local", "password123", "Other Owner", "user")
    create_workspace("owner-verify-workspace", owner_user_id="owner-verify", name="Owner Verify Workspace")
    create_workspace("other-verify-workspace", owner_user_id="other-workspace-owner", name="Other Verify Workspace")
    create_agent(
        agent_id="owner-verify-agent",
        display_name="Owner Verify Agent",
        owner_user_id="owner-verify",
        read_scopes=["agent:owner-verify-agent", "workspace:owner-verify-workspace", "user:owner-verify"],
        write_scopes=["agent:owner-verify-agent", "workspace:owner-verify-workspace"],
    )
    owner_session = create_session("owner-verify", channel="dashboard")
    agent_setup_client.cookies.set("session_token", owner_session["session_id"])
    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "owner-verify", "workspace_id": "other-verify-workspace", "agent_id": "owner-verify-agent"},
    )
    assert r.status_code == 403


def test_verify_endpoint_missing_project(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "nonexistent-workspace", "agent_id": "claude-code"},
    )
    assert r.status_code == 404


def test_verify_endpoint_missing_agent(agent_setup_client):
    r = agent_setup_client.post(
        "/api/agent-setup/verify",
        json={"user_id": "brian", "workspace_id": "agent-core", "agent_id": "nonexistent-agent"},
    )
    assert r.status_code == 404


def test_verify_button_visible_when_context_selected(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert 'id="verify-btn"' in html
    assert "Run Setup Check" in html


def test_agent_setup_claude_md_includes_claude_code_notes(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "Claude Code Notes" in html
    assert "AGENT_CORE_API_KEY" in html
    assert "configured MCP connection" in html
    assert "key determines which Agent Core user and agent are active" in html


def test_agent_setup_agents_md_includes_codex_notes(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=codex&output_type=agents_md"
    )
    assert r.status_code == 200
    html = r.text
    assert "Codex Notes" in html
    assert "AGENTS.md" in html
    assert "MCP tools" in html
    assert "as `opencode` for Brian" not in html
    assert "as `claude-code` for Brian" not in html


def test_agent_setup_instructions_are_artifact_based_for_legacy_target(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=cursor&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "What To Generate" in html
    assert "MCP Config" in html
    assert "CLAUDE.md" in html
    assert "AGENTS.md" in html
    assert "Cursor Tips" not in html


def test_agent_setup_verify_js_reads_memory_checkbox_in_verify_handler(agent_setup_client):
    r = agent_setup_client.get(
        "/agent-setup?user_id=brian&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    verify_start = html.index("async function runSetupVerification()")
    verify_end = html.index("const r = await fetch('/api/agent-setup/verify'", verify_start)
    verify_preamble = html[verify_start:verify_end]
    assert "const writeTestMemory = document.getElementById('write-test-memory')" in verify_preamble
