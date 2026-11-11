import pytest
import re

from app.branding import ENV_PREFIX
from app.services.auth_service import create_user, create_session, get_user_by_id
from app.services.agent_service import create_agent
from app.services.workspace_service import create_workspace
from app.services.credential_service import create_credential
from app.services import activity_service
from tests.conftest import csrf_headers


@pytest.fixture
def setup_integrations_data(clean_db):
    create_user("alex", "alex@test.local", "password123", "Alex", "admin")
    session = create_session("alex", channel="dashboard")
    user = get_user_by_id("alex")

    create_workspace(
        "agent-core",
        owner_user_id="alex",
        name="Agent Core",
        description="Self-hosted agent infrastructure",
    )

    create_agent(
        agent_id="claude-code",
        display_name="Claude Code",
        owner_user_id="alex",
        # Principal user read access is derived at request time, not stored.
        read_scopes=["agent:claude-code", "workspace:agent-core"],
        write_scopes=["agent:claude-code", "workspace:agent-core"],
    )

    create_credential(
        scope="workspace:agent-core",
        name="github-token",
        value_plaintext="ghp_secret",
        label="GitHub Token",
        created_by="alex",
    )

    activity_service.create_activity(
        agent_id="claude-code",
        user_id="alex",
        task_description="Test task for Agent Core",
        memory_scope="workspace:agent-core",
    )

    return {"user_id": "alex", "user": user, "session_id": session["session_id"]}


@pytest.fixture
def integrations_client(test_client, setup_integrations_data):
    test_client.cookies.set("session_token", setup_integrations_data["session_id"])
    return test_client


def test_integrations_page_loads(integrations_client):
    r = integrations_client.get("/integrations")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Connect</h1>" in html
    assert "Generate setup instructions" in html
    assert 'href="/integrations?' in html
    assert 'id="user_id"' in html
    assert 'id="workspace_id"' in html
    assert 'id="agent_id"' in html
    assert 'id="target"' not in html
    assert 'id="output_type"' not in html
    assert "setup-tabs" in html
    assert 'data-setup-path="connection"' in html
    assert 'data-setup-path="workspace-instructions"' in html
    assert "1. Connect a client" in html
    assert "2. Add or refresh workspace instructions" in html
    assert "Connection doctor" in html
    assert 'id="test-connection-details-btn"' in html
    assert "/mcp" in html


def test_integrations_keeps_connection_setup_separate_from_workspace_guidance(
    integrations_client,
):
    common = (
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code"
        "&target=claude_code&output_type="
    )

    instruction_page = integrations_client.get(common + "agents_md")
    assert instruction_page.status_code == 200
    assert "You can refresh them" in instruction_page.text
    assert "id='generate-connection-btn'" not in instruction_page.text
    assert "Refresh Preview" in instruction_page.text

    connection_page = integrations_client.get(common + "mcp_json")
    assert connection_page.status_code == 200
    assert "Generating one creates a new key for the selected agent" in connection_page.text
    assert "id='generate-connection-btn'" in connection_page.text


def test_agent_setup_extra_js_exposes_the_exact_mcp_endpoint():
    from app.routes.integrations_page import _agent_setup_extra_js

    scripts = _agent_setup_extra_js("https://core.example.test")
    assert 'window._AC_MCP_URL = "https://core.example.test/mcp"' in scripts


def test_integrations_shows_selectors(integrations_client):
    r = integrations_client.get("/integrations")
    assert r.status_code == 200
    html = r.text
    assert 'value="alex"' in html
    assert 'value="agent-core"' in html
    assert 'value="claude-code"' in html


def test_integrations_sorts_context_selectors_for_the_current_user(test_client, clean_db):
    create_user("zoe", "zoe@test.local", "password123", "Zoe", "admin")
    session = create_session("zoe", channel="dashboard")
    create_user("adam", "adam@test.local", "password123", "Adam")
    create_user("maya", "maya@test.local", "password123", "Maya")

    create_workspace("zeta", owner_user_id="zoe", name="Zeta Workspace")
    create_workspace("alpha", owner_user_id="zoe", name="Alpha Workspace")
    create_agent(
        agent_id="zeta-agent",
        display_name="Zeta Agent",
        owner_user_id="zoe",
        read_scopes=["agent:zeta-agent"],
        write_scopes=["agent:zeta-agent"],
    )
    create_agent(
        agent_id="alpha-agent",
        display_name="Alpha Agent",
        owner_user_id="zoe",
        read_scopes=["agent:alpha-agent"],
        write_scopes=["agent:alpha-agent"],
    )

    test_client.cookies.set("session_token", session["session_id"])
    html = test_client.get("/integrations").text

    def selector_values(selector_id):
        selector = re.search(
            rf'<select id="{selector_id}".*?</select>', html, re.S
        ).group(0)
        return re.findall(r'<option value="([^"]*)"', selector)

    assert selector_values("user_id") == ["zoe", "adam", "maya"]
    assert selector_values("workspace_id") == ["", "alpha", "zeta"]
    assert selector_values("agent_id") == ["", "alpha-agent", "zeta-agent"]


def test_integrations_generates_claude_md(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
    )
    assert r.status_code == 200
    html = r.text
    # The page leads with the chosen tool's steps; the requested raw output is under "Other outputs".
    output_match = re.search(r"<pre class='output-block'>(.*?)</pre>", html.split('id="other-outputs"', 1)[1], re.S)
    assert output_match is not None
    output = output_match.group(1)
    assert "CLAUDE.md" in html
    assert "open a fresh activity first" in output
    assert "task_note" in output
    assert "workspace:agent-core" in output
    assert "task_result" in output
    assert "user:alex" not in output
    assert "Agent ID:" not in output


def test_full_instruction_templates_include_memory_discipline_guidance():
    """Long-form repository instructions must carry the anti-repetition memory
    rule ("one memory per insight, not per occurrence"). Added after three
    different live agents each wrote one fact/decision record per monitor
    tick / watchdog refire / idle heartbeat (140+ near-duplicates), because
    the generic "no routine progress" line didn't cover findings-shaped
    repetition."""
    from app.routes.integrations_page import (
        MEMORY_DISCIPLINE_GUIDANCE,
        _build_agents_md,
        _build_claude_md,
        _build_instructions,
        _build_session_prompt,
    )

    needle = "One memory per insight, not per occurrence"
    assert needle in MEMORY_DISCIPLINE_GUIDANCE

    outputs = {
        "instructions": _build_instructions(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "session_prompt": _build_session_prompt(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "claude_md": _build_claude_md(
            "http://x", "user:u", "workspace:w", "agent:a", "Agent", "ws"
        ),
        "agents_md": _build_agents_md(
            "http://x", "user:u", "workspace:w", "agent:a", "ws"
        ),
    }
    for name, output in outputs.items():
        assert needle in output, f"{name} is missing the memory-discipline guidance"
        assert "{MEMORY_DISCIPLINE_GUIDANCE}" not in output, (
            f"{name} has an unresolved placeholder"
        )


def test_full_instruction_templates_require_activity_scope_continuity():
    """Long-form repository instructions must keep lifecycle updates in scope."""
    from app.routes.integrations_page import (
        _build_agents_md,
        _build_claude_md,
        _build_instructions,
        _build_session_prompt,
    )

    outputs = {
        "instructions": _build_instructions(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "session_prompt": _build_session_prompt(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "claude_md": _build_claude_md(
            "http://x", "user:u", "workspace:w", "agent:a", "Agent", "ws"
        ),
        "agents_md": _build_agents_md(
            "http://x", "user:u", "workspace:w", "agent:a", "ws"
        ),
    }
    for name, output in outputs.items():
        assert "never moves one across scopes" in output, (
            f"{name} is missing activity scope-continuity guidance"
        )


def test_full_instruction_templates_require_workspace_sync_lifecycle():
    """Long-form repository instructions must teach the sync contract."""
    from app.routes.integrations_page import (
        _build_agents_md,
        _build_claude_md,
        _build_instructions,
        _build_session_prompt,
    )

    outputs = {
        "instructions": _build_instructions(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "session_prompt": _build_session_prompt(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "claude_md": _build_claude_md(
            "http://x", "user:u", "workspace:w", "agent:a", "Agent", "ws"
        ),
        "agents_md": _build_agents_md(
            "http://x", "user:u", "workspace:w", "agent:a", "ws"
        ),
    }
    for name, output in outputs.items():
        for required in (
            "workspace_sync",
            "workspace_sync_ack",
            "execution_id",
            "stable change ID",
            "other_session_changes",
            "intentionally skip",
            "Workspace context is not repository access",
            "Pass the session's `execution_id` to supported writes",
            "sync again until `has_more` is false",
            "If `cursor_reset` is true",
            "stop and report the sync failure",
        ):
            assert required in output, f"{name} is missing {required} guidance"


def test_claude_generator_preloads_workspace_sync_schemas():
    from app.routes.integrations_page import _build_claude_md

    output = _build_claude_md(
        "http://x", "user:u", "workspace:w", "agent:a", "Agent", "ws"
    )
    preload = (
        'ToolSearch("select:mcp__agent-core__session_context,mcp__agent-core__workspace_sync,'
        'mcp__agent-core__workspace_sync_ack,'
        'mcp__agent-core__memory_search,'
        'mcp__agent-core__activity_update,'
        'mcp__agent-core__memory_write")'
    )
    assert preload in output


def test_full_instruction_templates_describe_principal_and_connector_authority():
    from app.routes.integrations_page import (
        CONNECTOR_BINDING_GUIDANCE,
        _build_agents_md,
        _build_claude_md,
        _build_instructions,
        _build_session_prompt,
    )

    outputs = {
        "instructions": _build_instructions(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "session_prompt": _build_session_prompt(
            "claude_code", "http://x", "user:u", "workspace:w", "agent:a",
            "Agent", "User", "ws",
        ),
        "claude_md": _build_claude_md(
            "http://x", "user:u", "workspace:w", "agent:a", "Agent", "ws"
        ),
        "agents_md": _build_agents_md(
            "http://x", "user:u", "workspace:w", "agent:a", "ws"
        ),
    }
    for name, output in outputs.items():
        assert "automatically inherit read access" in output, name
        assert "when you have user-scope read access" not in output, name
        assert CONNECTOR_BINDING_GUIDANCE in output, name


def test_integrations_generates_env_vars(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert f"{ENV_PREFIX}URL" in html
    assert f"{ENV_PREFIX}API_KEY" in html
    assert f"{ENV_PREFIX}WORKSPACE_SCOPE" in html
    assert "workspace:agent-core" in html


def test_integrations_project_is_optional_for_env(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&agent_id=claude-code&target=claude_code&output_type=env"
    )
    assert r.status_code == 200
    html = r.text
    assert "-- Optional --" in html
    assert f"{ENV_PREFIX}AGENT_ID" in html
    assert f"{ENV_PREFIX}USER_SCOPE" in html
    assert "workspace:your-workspace-id" in html


def test_integrations_generates_verification_prompt(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=generic_mcp&output_type=verification"
    )
    assert r.status_code == 200
    html = r.text
    assert "workspace:agent-core" in html
    assert "verification" in html.lower() or "Verify" in html
    assert "workspace_sync" in html
    assert "workspace_sync_ack" in html
    assert "execution_id" in html
    assert "activity_update" in html
    assert "memory_get" in html
    assert "`scope` set to `workspace:agent-core`" in html
    assert "`view` set to `full`" in html
    assert "returned records include the captured record ID" in html
    assert "memory_get` for the record ID" not in html
    assert "credential_list" in html
    assert "connectors_list" in html
    assert "connectors_bindings_list" in html
    assert "connectors_actions_list" in html
    assert "connectors_bindings_test" in html
    assert "connectors_summary" in html
    assert "task_result" in html


def test_verification_prompt_uses_the_real_memory_get_schema():
    from app.routes.integrations_page import _build_verification_prompt
    from app.routes.mcp import MANIFEST

    memory_get = next(
        tool for tool in MANIFEST["tools"] if tool["name"] == "memory_get"
    )
    properties = memory_get["inputSchema"]["properties"]
    prompt = _build_verification_prompt("user:u", "workspace:w")

    assert "scope" in properties
    assert "view" in properties
    assert "record_id" not in properties
    assert "`scope` set to `workspace:w`" in prompt
    assert "`view` set to `full`" in prompt
    assert "memory_get` for the record ID" not in prompt
    assert "Call it again with `scope` set to `user:u`" in prompt
    assert "If you can read `user:u`" not in prompt


def test_integrations_access_checks_show_ok_for_good_agent(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "check-ok" in html
    assert "Agent active" in html
    assert "Workspace read/write access" in html
    assert "User preference read access" in html


def test_integrations_no_raw_credential_values_in_output(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    )
    assert r.status_code == 200
    html = r.text
    assert "ghp_secret" not in html


def test_integrations_generate_connection_endpoint(integrations_client):
    r = integrations_client.post(
        "/api/integrations/generate-connection",
        json={"user_id": "alex", "workspace_id": "agent-core", "agent_id": "claude-code", "output_type": "env"},
        headers=csrf_headers(integrations_client),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["api_key"]
    assert f"{ENV_PREFIX}URL" in data["output"]


def test_integrations_preview_and_apply_access_endpoints(integrations_client):
    preview = integrations_client.post(
        "/api/integrations/preview",
        json={
            "user_id": "alex",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "target": "claude_code",
            "output_type": "claude_md",
        },
        headers=csrf_headers(integrations_client),
    )
    assert preview.status_code == 200
    preview_data = preview.json()["data"]
    assert "recommended_scopes" in preview_data
    assert "outputs" in preview_data

    apply_access = integrations_client.post(
        "/api/integrations/apply-access",
        json={
            "user_id": "alex",
            "workspace_id": "agent-core",
            "agent_id": "claude-code",
            "include_user_write": False,
        },
        headers=csrf_headers(integrations_client),
    )
    assert apply_access.status_code == 200
    assert "read_scopes" in apply_access.json()["data"]


def test_integrations_page_surfaces_artifact_validation(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=mcp_json"
    )
    assert r.status_code == 200
    html = r.text
    assert "Check Setup" not in html
    assert "Artifact validation:" not in html


def test_generating_a_repo_instruction_file_does_not_rotate_the_key(test_client, admin_token):
    """Generating AGENTS.md must not knock a running agent offline.

    The file contains no agent-specific content, but every generate call used to
    rotate the selected agent's key, invalidating whatever a live session was
    already using.
    """
    from app.services import agent_service

    test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "rotation-probe", "display_name": "Probe", "description": "t"},
    )
    first = test_client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "rotation-probe", "output_type": "env"},
    )
    assert first.status_code == 200, first.json()
    key_hash_before = agent_service.get_agent_by_id("rotation-probe")["api_key_hash"]

    for output_type in ("agents_md", "claude_md", "instructions", "session"):
        r = test_client.post(
            "/api/integrations/generate-connection",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "user_id": "admin",
                "agent_id": "rotation-probe",
                "output_type": output_type,
            },
        )
        assert r.status_code == 200, f"{output_type}: {r.json()}"
        after = agent_service.get_agent_by_id("rotation-probe")["api_key_hash"]
        assert after == key_hash_before, f"{output_type} rotated the agent's key"


def test_generating_a_connection_config_still_rotates(test_client, admin_token):
    """The key-bearing outputs must still issue a fresh key."""
    from app.services import agent_service

    test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "rotation-probe-2", "display_name": "Probe 2", "description": "t"},
    )
    test_client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "rotation-probe-2", "output_type": "env"},
    )
    before = agent_service.get_agent_by_id("rotation-probe-2")["api_key_hash"]

    for output_type in ("mcp_json", "env"):
        r = test_client.post(
            "/api/integrations/generate-connection",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "user_id": "admin",
                "agent_id": "rotation-probe-2",
                "output_type": output_type,
            },
        )
        assert r.status_code == 200, r.json()
        after = agent_service.get_agent_by_id("rotation-probe-2")["api_key_hash"]
        assert after != before, f"{output_type} should have issued a fresh key"
        before = after


def test_the_form_says_what_the_agent_choice_affects(test_client, admin_token):
    """Rotation is invisible otherwise: nothing on screen said generating a
    config would invalidate the key a running session is using."""
    r = test_client.get(
        "/integrations", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert "only updates the preview" in r.text
    assert "creates a new key for the selected agent" in r.text
    assert "the same for every agent" in r.text


def test_connect_is_three_steps_and_a_chosen_tool_gets_only_its_own_pieces(integrations_client):
    from app.routes.integrations_page import _build_mcp_json, SETUP_TOOLS

    # The full MCP config is byte-for-byte what it was; per-tool pieces are cut from it.
    full = _build_mcp_json("https://core.test", "K")
    assert full.startswith("# Claude Code") and "# Codex CLI" in full and "# OpenCode" in full
    codex_only = _build_mcp_json("https://core.test", "K", tool="codex")
    assert codex_only in full and "# Claude Code" not in codex_only and "mcpServers" not in codex_only
    assert "# Codex CLI" not in _build_mcp_json("https://core.test", "K", tool="claude_code")
    assert '"serverUrl"' in _build_mcp_json("https://core.test", "K", tool="antigravity")
    assert [t[0] for t in SETUP_TOOLS] == ["claude_code", "codex", "cursor", "windsurf", "antigravity", "generic_mcp"]


def test_connect_page_lists_the_steps_and_keeps_every_other_output(test_client, admin_token):
    r = test_client.get("/integrations", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    html = r.text
    assert html.index("1. Who is connecting?") < html.index("2. Which tool?") < html.index("3. Check it works")
    assert "Choose your tool to see exactly what to copy" in html
    assert 'id="other-outputs"' in html and "Next Steps" not in html
    for label in ("Claude Code", "Codex", "Cursor", "Windsurf", "Antigravity", "Other MCP tool"):
        assert f">{label}</a>" in html


def test_a_chosen_tool_shows_its_connect_block_its_file_and_the_key_button(integrations_client):
    base = "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code"
    claude = integrations_client.get(base + "&target=claude_code").text
    steps = claude.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0]
    assert "Connect it" in steps and "Give it instructions" in steps and "Optional: add context" in steps
    assert "# Claude Code" in steps and "# Codex CLI" not in steps
    assert "id='generate-tool-connection-btn'" in steps and "data-output-type='mcp_tool'" in steps
    assert "CLAUDE.md" in steps

    codex = integrations_client.get(base + "&target=codex").text.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0]
    assert "# Codex CLI" in codex and "# Claude Code" not in codex and "AGENTS.md" in codex
    assert "Optional: add context" not in codex

    other = integrations_client.get(base).text.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0]
    assert "Choose your tool to see exactly what to copy" in other and "Connect it" not in other
    # Step 3 carries the verification prompt once an agent is chosen.
    assert "Verification Prompt" in integrations_client.get(base + "&target=codex").text.split('id="check-step"', 1)[1]


def test_generating_a_key_for_the_chosen_tool_returns_only_that_tools_config(integrations_client):
    r = integrations_client.post("/api/integrations/generate-connection", json={
        "user_id": "alex", "workspace_id": "agent-core", "agent_id": "claude-code",
        "target": "codex", "output_type": "mcp_tool"}, headers=csrf_headers(integrations_client))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "# Codex CLI" in data["output"] and "# Claude Code" not in data["output"]
    assert data["api_key"] and data["api_key"] in data["output"]


def _generate(client, **extra):
    body = {"user_id": "alex", "workspace_id": "agent-core", "agent_id": "claude-code", "output_type": "claude_md"}
    body.update(extra)
    return client.post("/api/integrations/generate-connection", json=body, headers=csrf_headers(client))


def test_generate_connection_refuses_an_unknown_output_type(integrations_client):
    r = _generate(integrations_client, output_type="mcp_config")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_OUTPUT_TYPE"
    assert "mcp_json" in r.json()["error"]["message"]


def test_every_listed_output_type_is_accepted(integrations_client):
    from app.routes.integrations_page import KNOWN_OUTPUT_TYPES, _agent_setup_output_options

    assert {o[0] for o in _agent_setup_output_options()} <= KNOWN_OUTPUT_TYPES
    assert _generate(integrations_client, output_type="verification").status_code == 200


def test_generated_files_name_only_the_tools_that_agent_can_use(integrations_client):
    create_agent(
        agent_id="notes-only",
        display_name="Notes only",
        owner_user_id="alex",
        read_scopes=["agent:notes-only", "workspace:agent-core"],
        write_scopes=["agent:notes-only", "workspace:agent-core"],
        capabilities=["memory", "coordination"],
    )
    narrow = {t: _generate(integrations_client, agent_id="notes-only", output_type=t).json()["data"]["output"]
              for t in ("claude_md", "agents_md", "session")}
    def listing(text):
        # The sentence that lists the tools; other prose may still mention a tool by name.
        return next(line for line in text.splitlines() if "MCP tools include" in line or "MCP tools you can use" in line)

    for text in narrow.values():
        line = listing(text)
        assert "`memory_search`" in line
        assert "approval_propose" not in line
        assert "`connectors_run`" not in line
        assert "`credential_get`" not in line
    full = _generate(integrations_client, output_type="claude_md").json()["data"]["output"]
    assert "`connectors_run`" in listing(full) and "approval_propose" not in listing(full)
    create_agent(
        agent_id="proposer-x",
        display_name="Proposer",
        owner_user_id="alex",
        read_scopes=["agent:proposer-x", "workspace:agent-core"],
        write_scopes=["agent:proposer-x"],
        capabilities=["memory", "admin_propose"],
    )
    proposer = _generate(integrations_client, agent_id="proposer-x").json()["data"]["output"]
    assert "`approval_propose`" in listing(proposer) and "`connectors_run`" not in listing(proposer)


def test_agent_update_refuses_a_field_it_does_not_know(integrations_client):
    r = integrations_client.put(
        "/api/agents/claude-code", json={"is_active": False}, headers=csrf_headers(integrations_client)
    )
    assert r.status_code == 422
    assert "is_active" in r.text
    assert integrations_client.put(
        "/api/agents/claude-code", json={"description": "still works"}, headers=csrf_headers(integrations_client)
    ).status_code == 200
