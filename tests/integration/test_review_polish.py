"""Review findings: small things that made the product read or behave worse than it should."""

from datetime import timedelta

from app.database import get_db
from app.services import activity_service, agent_service, audit_service, memory_service, workspace_service
from app.services.activity_service import utc_now
from tests.conftest import csrf_headers

WS = "workspace:rp-a"


def _agent(test_client):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("rp-a", "Billing", owner)
    _, key = agent_service.create_agent(agent_id="polisher", display_name="polisher", owner_user_id=owner, read_scopes=[WS, "agent:polisher"], write_scopes=[WS, "agent:polisher"])
    return key


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def test_a_handoff_accepts_text_or_a_list_for_every_field(test_client, admin_token):
    key = _agent(test_client)
    ex = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="p1")["execution_id"]
    out = _mcp(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Tidy the docs",
               status="completed", task_result="Tidied.", handoff={"completed": ["Fixed two links", "Removed a stale page"], "next_steps": "Review the index", "open_questions": ["Keep the FAQ?"]})
    stored = activity_service.load_handoff(activity_service.get_activity(out["activity"]["id"])["handoff_json"])
    assert stored["completed"] == "Fixed two links; Removed a stale page" and stored["next_steps"] == ["Review the index"] and stored["open_questions"] == ["Keep the FAQ?"]


def test_the_activity_update_schema_offers_only_the_statuses_an_agent_may_set(test_client, admin_token):
    key = _agent(test_client)
    manifest = test_client.get("/mcp", headers={"Authorization": f"Bearer {key}"}).json()
    tools = (manifest.get("data") or manifest)["tools"]
    update = next(t for t in tools if t["name"] == "activity_update")
    assert set(update["inputSchema"]["properties"]["status"]["enum"]) == {"active", "completed", "blocked", "cancelled"}


def test_memory_detail_has_no_importance_and_the_list_shows_author_and_date_and_cuts_at_a_word(test_client, admin_token):
    key = _agent(test_client)
    long_text = "Invoice exports use semicolon-delimited CSV, " + "and the finance import rejects commas inside quoted names " * 6
    _mcp(test_client, key, "memory_write", content=long_text, memory_class="fact", scope=WS, topic="invoice-export-format")
    page = test_client.get("/memory").text
    assert "mem-detail-importance" not in page
    row = page.split('id="mem-results-body"', 1)[1].split("</tbody>", 1)[0]
    assert "by polisher" in row
    shown = row.split("mem-content'>", 1)[1].split("<br>", 1)[0]
    assert shown.endswith("…")
    assert shown[:-1].split()[-1] in long_text.split(), shown  # ends on a whole word


def test_an_abandoned_task_keeps_the_time_of_its_last_update_not_the_sweep_time(test_client, admin_token):
    key = _agent(test_client)
    task = _mcp(test_client, key, "activity_update", memory_scope=WS, task_description="Investigate the slow import", status="active")["activity"]
    long_ago = (utc_now() - timedelta(hours=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET updated_at = ?, heartbeat_at = ? WHERE id = ?", (long_ago, long_ago, task["id"]))
        conn.commit()
    assert activity_service.close_abandoned_activities() == 1
    row = activity_service.get_activity(task["id"])
    assert row["status"] == "cancelled" and row["updated_at"] == long_ago and row["ended_at"] == long_ago


def test_the_verification_pass_writes_an_audit_entry_only_when_it_found_something(clean_db):
    from app.services import backup_service

    before = len(audit_service.query_events(action="memory_verified", limit=100))
    backup_service.run_scheduled_maintenance()
    backup_service.run_scheduled_maintenance()
    assert len(audit_service.query_events(action="memory_verified", limit=100)) == before  # an uneventful pass is not an event


def test_a_memory_an_owner_saves_through_the_api_is_not_stamped_as_an_agents_guess(test_client, admin_token):
    key = _agent(test_client)
    r = test_client.post("/api/memory/write", headers=csrf_headers(test_client), json={"content": "The owner decided to keep the FAQ page.", "memory_class": "decision", "scope": WS})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["record"]["source_kind"] == "operator_authored"
    agent_write = _mcp(test_client, key, "memory_write", content="Exports are generated nightly at two in the morning.", memory_class="fact", scope=WS)
    assert agent_write["record"]["source_kind"] == "agent_inference"


def test_needs_you_does_not_say_agents_are_working_when_there_are_none_and_registration_states_the_minimum(test_client, admin_token):
    page = test_client.get("/").text
    assert "Your agents are working" not in page and "Add an agent" in page
    from app.routes import auth_pages
    assert "At least 8 characters" in open(auth_pages.__file__).read()


def test_two_agents_created_with_defaults_in_a_one_workspace_install_share_it(test_client, admin_token):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("only-one", "Only One", owner)
    headers = csrf_headers(test_client)
    for name in ("first-agent", "second-agent"):
        r = test_client.post("/api/agents", headers=headers, json={"id": name, "display_name": name})
        assert r.status_code in (200, 201), r.text
        agent = agent_service.get_agent_by_id(name)
        assert "workspace:only-one" in agent_service.parse_scopes(agent["read_scopes_json"])
        assert "workspace:only-one" in agent_service.parse_scopes(agent["write_scopes_json"])
    # With two workspaces the default stays private: the person has to choose.
    workspace_service.create_workspace("another", "Another", owner)
    r = test_client.post("/api/agents", headers=headers, json={"id": "third-agent", "display_name": "third"})
    third = agent_service.get_agent_by_id("third-agent")
    assert "workspace:only-one" not in agent_service.parse_scopes(third["write_scopes_json"])


def test_the_create_agent_dialog_shows_workspace_access_in_the_open_and_ticks_the_only_workspace(test_client, admin_token):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("solo", "Solo Project", owner)
    html = test_client.get("/agents").text
    dialog = html.split('id="create-agent-modal"', 1)[1].split('id="edit-agent-modal"', 1)[0]
    assert "Which workspace will it work in?" in dialog
    simple = dialog.split("Which workspace will it work in?", 1)[1].split("<details", 1)[0]
    assert "Solo Project" in simple and "data-simple-workspace" in simple and "checked" in simple  # one workspace: ticked for you
    assert "Advanced access" in dialog  # the separate read/write ticks are behind a fold, not the only way in


def test_connect_preselects_the_only_workspace_and_the_key_button_comes_before_the_config(test_client, admin_token):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("solo-connect", "Solo Connect", owner)
    agent_service.create_agent(agent_id="tool-agent", display_name="tool-agent", owner_user_id=owner)
    html = test_client.get("/integrations?agent_id=tool-agent&target=claude_code").text
    assert 'value="solo-connect" selected' in html
    step = html.split('id="tool-step"', 1)[1].split('id="check-step"', 1)[0]
    assert step.index("generate-tool-connection-btn") < step.index("output-block")  # the key button is first in reading order


def test_open_event_streams_end_when_the_server_is_told_to_stop(test_client, admin_token):
    from app.services.event_stream_service import event_hub

    event_hub.closing = True
    try:
        with test_client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            assert list(response.iter_lines()) == []  # the stream ends instead of holding shutdown up
    finally:
        event_hub.closing = False


def test_deleting_an_agent_cancels_what_it_left_waiting_on_a_person(test_client, admin_token):
    from app.services import credential_request_service

    key = _agent(test_client)
    _mcp(test_client, key, "credential_request", scope=WS, name="NEEDED_TOKEN", purpose="for the build")
    assert credential_request_service.list_pending()
    assert agent_service.delete_agent_hard("polisher")
    assert credential_request_service.list_pending() == []
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_executions WHERE agent_id = 'polisher'").fetchone()[0] == 0
