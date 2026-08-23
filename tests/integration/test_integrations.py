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
    assert 'value="alex"' in html
    assert 'value="agent-core"' in html
    assert 'value="claude-code"' in html


def test_integrations_generates_claude_md(integrations_client):
    r = integrations_client.get(
        "/integrations?user_id=alex&workspace_id=agent-core&agent_id=claude-code&target=claude_code&output_type=claude_md"
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
    assert "user:alex" not in output
    assert "Agent ID:" not in output


def test_all_generated_prompts_include_memory_discipline_guidance():
    """Every generated agent artifact must carry the anti-repetition memory
    rule ("one memory per insight, not per occurrence"). Added after three
    different live agents each wrote one fact/decision record per monitor
    tick / watchdog refire / idle heartbeat (140+ near-duplicates), because
    the generic "no routine progress" line didn't cover findings-shaped
    repetition."""
    from app.routes.integrations_page import (
        MEMORY_DISCIPLINE_GUIDANCE,
        _build_agents_md,
        _build_assistants_md,
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
        "assistants_md": _build_assistants_md(
            "http://x", "user:u", "workspace:w", "agent:a"
        ),
    }
    for name, output in outputs.items():
        assert needle in output, f"{name} is missing the memory-discipline guidance"
        assert "{MEMORY_DISCIPLINE_GUIDANCE}" not in output, (
            f"{name} has an unresolved placeholder"
        )


def test_all_generated_prompts_require_activity_scope_continuity():
    """Every client must keep lifecycle updates in the activity's scope."""
    from app.routes.integrations_page import (
        _build_agents_md,
        _build_assistants_md,
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
        "assistants_md": _build_assistants_md(
            "http://x", "user:u", "workspace:w", "agent:a"
        ),
    }
    for name, output in outputs.items():
        assert "never moves one across scopes" in output, (
            f"{name} is missing activity scope-continuity guidance"
        )


def test_all_generated_prompts_require_workspace_sync_lifecycle():
    """Every agent format must teach the same execution and cursor contract."""
    from app.routes.integrations_page import (
        _build_agents_md,
        _build_assistants_md,
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
        "assistants_md": _build_assistants_md(
            "http://x", "user:u", "workspace:w", "agent:a"
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
        'ToolSearch("select:mcp__agent-core__workspace_sync,'
        'mcp__agent-core__workspace_sync_ack,'
        'mcp__agent-core__memory_search,'
        'mcp__agent-core__activity_update,'
        'mcp__agent-core__memory_write")'
    )
    assert preload in output


def test_all_generated_prompts_describe_principal_and_connector_authority():
    from app.routes.integrations_page import (
        CONNECTOR_BINDING_GUIDANCE,
        _build_agents_md,
        _build_assistants_md,
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
        "assistants_md": _build_assistants_md(
            "http://x", "user:u", "workspace:w", "agent:a"
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
    assert "only re-renders the preview" in r.text
    assert "mints a fresh API key" in r.text
    assert "identical for every agent" in r.text
