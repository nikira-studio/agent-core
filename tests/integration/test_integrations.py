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
    # One flat list of other outputs; the old two-group sub-flow repeated the guided steps.
    assert "data-setup-path" not in html
    assert "1. Connect a client" not in html and "2. Add or refresh workspace instructions" not in html
    assert "Only what the steps above do not already give you" in html
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
    assert "Only what the steps above do not already give you" in instruction_page.text
    assert "id='generate-connection-btn'" not in instruction_page.text
    assert "Refresh Preview" in instruction_page.text

    connection_page = integrations_client.get(common + "mcp_json")
    assert connection_page.status_code == 200
    assert "creates a new key and replaces the agent's old one" in connection_page.text
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
        "/integrations?user_id=alex&agent_id=claude-code&target=claude_code&workspace_id=&output_type=env"
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
    assert "memory_search" in html
    assert "activity_update" in html
    assert "task_result" in html
    assert "connectors_bindings_list" not in html  # four steps, not a tour of every tool


def test_verification_prompt_is_four_steps_in_one_scope():
    import re

    from app.routes.integrations_page import _build_verification_prompt
    from app.routes.mcp import MANIFEST

    tools = {tool["name"]: tool for tool in MANIFEST["tools"]}
    prompt = _build_verification_prompt("workspace:w")
    assert [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)] == [1, 2, 3, 4]
    assert prompt.count("`workspace:w`") >= 4 and "user:" not in prompt
    assert "scope" in tools["memory_search"]["inputSchema"]["properties"]
    assert "task_result" in prompt and "memory_search" in prompt and "activity_update" in prompt


def test_with_no_workspace_the_verification_uses_the_agents_private_scope(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&agent_id=claude-code&target=generic_mcp&workspace_id=&output_type=verification"
    )
    assert r.status_code == 200
    prompt = r.text.split("Check that", 1)[1].split("Use the full prefixed scope name", 1)[0]
    assert "`agent:claude-code`" in prompt and "user:alex" not in prompt


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
    assert "replaces that agent's old key" in r.text
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
    assert "Connect it" in steps and "Give it instructions" in steps and "Add the session start hook" in steps
    assert "# Claude Code" in steps and "# Codex CLI" not in steps
    assert "id='generate-tool-connection-btn'" in steps and "data-output-type='mcp_tool'" in steps
    assert "CLAUDE.md" in steps

    codex = integrations_client.get(base + "&target=codex").text.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0]
    assert "# Codex CLI" in codex and "# Claude Code" not in codex and "AGENTS.md" in codex
    # The hook is in the step itself, for this tool only, with both operating systems.
    assert "Add the session start hook" in codex and "Other outputs → Codex Hook" not in codex
    assert "hooks.json" in codex and "commandWindows" in codex and "/hooks" in codex
    assert "Claude Code" not in codex.split("Add the session start hook", 1)[1] and "settings.json" not in codex
    assert "settings.json" in claude.split('id="tool-step"', 1)[1] and "commandWindows" not in steps
    assert "data-os='windows'" in steps and "data-os='unix'" in steps and "Test the hook by hand" in steps

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




def test_agent_update_refuses_a_field_it_does_not_know(integrations_client):
    r = integrations_client.put(
        "/api/agents/claude-code", json={"is_active": False}, headers=csrf_headers(integrations_client)
    )
    assert r.status_code == 422
    assert "is_active" in r.text
    assert integrations_client.put(
        "/api/agents/claude-code", json={"description": "still works"}, headers=csrf_headers(integrations_client)
    ).status_code == 200


def test_the_connection_doctor_sends_no_cookies():
    """With the dashboard cookie it would be signed in and always report that no bearer key is required."""
    import pathlib

    source = pathlib.Path("app/dashboard/static/js/integrations.js").read_text()
    body = source.split("async function testConnectionDetails", 1)[1].split("\nfunction ", 1)[0]
    assert body.count("fetch(mcpUrl") == 2 and body.count("credentials: 'omit'") == 2


def test_a_failed_access_check_offers_give_access(integrations_client):
    create_agent(
        agent_id="no-access", display_name="No Access", owner_user_id="alex",
        read_scopes=["agent:no-access"], write_scopes=["agent:no-access"],
    )
    html = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=no-access&target=claude_code&output_type=instructions"
    ).text
    assert "check-blocked" in html or "check-warn" in html
    assert ">Give access</button>" in html and "applyRecommendedAccess()" in html
    good = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=instructions"
    ).text
    assert ">Give access</button>" not in good


def test_the_docs_name_both_hooks_and_the_read_access_a_mapped_folder_needs():
    import pathlib

    doc = pathlib.Path("docs/integrations.md").read_text()
    assert "| Claude Code Hook |" in doc and "| Codex Hook |" in doc
    assert "**Session start hooks.**" in doc
    paragraph = doc.split("**Session start hooks.**", 1)[1].split("**Refresh workspace guidance", 1)[0]
    assert "Repository folder" in paragraph and "read access" in paragraph and "exactly like an unmapped one" in paragraph


def _steps(client, target, agent="claude-code"):
    base = f"/integrations?user_id=alex&workspace_id=agent-core&agent_id={agent}&target={target}"
    html = client.get(base).text
    return html, html.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0], html.split('id="other-outputs"', 1)[1]


def test_other_outputs_do_not_repeat_what_the_chosen_tools_steps_show(integrations_client):
    from app.routes.integrations_page import _agent_setup_output_options

    everything = {label for _v, label, _f in _agent_setup_output_options()}
    _html, _steps_html, other = _steps(integrations_client, "codex")
    tabs = set(re.findall(r'<a class="setup-tab [^"]*" href="[^"]*">([^<]+)</a>', other))
    # Codex's own file, its hook and the verification prompt are in the steps; everything else stays.
    assert tabs == everything - {"AGENTS.md", "Codex Hook", "Verification Prompt"}
    _html, _steps_html, other = _steps(integrations_client, "claude_code")
    tabs = set(re.findall(r'<a class="setup-tab [^"]*" href="[^"]*">([^<]+)</a>', other))
    assert tabs == everything - {"CLAUDE.md", "Claude Code Hook", "Verification Prompt"}
    # A tool without a hook of its own sees both hooks as "other tools' files".
    _html, _steps_html, other = _steps(integrations_client, "cursor")
    tabs = set(re.findall(r'<a class="setup-tab [^"]*" href="[^"]*">([^<]+)</a>', other))
    assert {"Claude Code Hook", "Codex Hook", "CLAUDE.md", "AGENTS.md"} <= tabs and "Instructions" not in tabs


def test_steps_say_whether_agent_core_has_seen_the_connection_and_the_hook(integrations_client):
    from app.services import audit_service

    _html, before, _o = _steps(integrations_client, "codex")
    assert "No connection seen yet" in before and "No session start seen yet" in before
    audit_service.write_event(actor_type="agent", actor_id="claude-code", action="memory_search",
                              resource_type="memory", resource_id="x", result="success")
    _html, connected, _o = _steps(integrations_client, "codex")
    assert "Connected: last seen just now" in connected and "No session start seen yet" in connected
    # The agent loading its own context over MCP (what AGENTS.md tells it to do) is not the hook running.
    audit_service.write_event(actor_type="agent", actor_id="claude-code", action="context_injected",
                              resource_type="execution", resource_id="e0", result="success",
                              details={"memory_scope": "workspace:agent-core", "resolved_by": "requested", "source": "mcp", "channel": "mcp"})
    _html, mcp_only, _o = _steps(integrations_client, "codex")
    assert "No session start seen yet" in mcp_only and "Hook working" not in mcp_only
    # A REST request with no session-start source is not a hook either.
    audit_service.write_event(actor_type="agent", actor_id="claude-code", action="context_injected",
                              resource_type="execution", resource_id="e0b", result="success",
                              details={"memory_scope": "workspace:agent-core", "resolved_by": "requested", "source": None, "channel": "rest"})
    assert "Hook working" not in _steps(integrations_client, "codex")[1]
    audit_service.write_event(actor_type="agent", actor_id="claude-code", action="context_injected",
                              resource_type="execution", resource_id="e1", result="success",
                              details={"memory_scope": "workspace:agent-core", "resolved_by": "cwd", "source": "startup", "channel": "rest"})
    _html, hooked, _o = _steps(integrations_client, "codex")
    assert "Hook working: context delivered just now" in hooked


def test_the_steps_tell_you_to_run_them_yourself_and_give_windows_commands(integrations_client):
    import json as _json

    _html, codex, _o = _steps(integrations_client, "codex")
    assert "Do steps a and c yourself, in a terminal" in codex and "without network access" in codex
    assert "Invoke-WebRequest" in codex and "WriteAllText" in codex and "Test-Path" in codex
    from app.routes.integrations_page import _hook_steps

    win = _json.loads(_hook_steps("codex", "https://core.test")["register"]["windows"])
    handler = win["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["commandWindows"] == 'python "C:/Users/YOU/.codex/hooks/agent_core_hook.py"'
    assert "~" not in handler["commandWindows"] and handler["command"].startswith("python3 ")
    claude = _json.loads(_hook_steps("claude_code", "https://core.test")["register"]["windows"])
    hook = claude["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook["command"] == "python" and hook["args"] == ["C:/Users/YOU/.claude/hooks/agent_core_hook.py"]
    # Existing settings are merged into, on every system.
    for key in ("claude_code", "codex"):
        steps = _hook_steps(key, "https://core.test")
        assert all("already exists" in steps["register_where"][os_] for os_ in ("unix", "windows"))
    _html, claude_steps, _o = _steps(integrations_client, "claude_code")
    assert "Do steps a and c yourself" in claude_steps


def test_codex_step_c_covers_the_desktop_app_and_a_workspace_with_no_repository_folder(integrations_client):
    from app.services import workspace_service

    _html, codex, _o = _steps(integrations_client, "codex")
    plain = codex.replace("<code>", "").replace("</code>", "").replace("&gt;", ">")
    assert "Settings > Hooks" in plain and "has no /hooks" in plain and "Review hooks" in plain
    assert "Agent Core has no Repository folder yet, so a session cannot be matched" in codex
    workspace_service.set_repo_root("agent-core", "C:\\Users\\alex\\media studio")
    _html, mapped, _o = _steps(integrations_client, "codex")
    assert "has no Repository folder yet" not in mapped
