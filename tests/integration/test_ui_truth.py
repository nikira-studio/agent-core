"""Dashboard text says what is true: health with nothing set up, who can use a credential, dialog titles."""

from app.services import agent_service, credential_service, connector_service, workspace_service
from app.database import get_db


def _admin(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def test_overview_health_says_none_set_up_without_services(test_client, admin_token):
    html = test_client.get("/", headers=_admin(admin_token)).text
    assert "none set up" in html and "all working" not in html


def _bound_service():
    connector_service.create_connector_type(
        connector_type_id="health-gen", display_name="Gen", auth_type="none",
        supported_actions=[{"name": "GET /x", "method": "GET"}], backend_type="generic_http",
        endpoint_url="https://api.example.com",
    )
    return connector_service.create_binding("health-gen", "B", "user:admin", config_json="{}")


def _services_tile(test_client, admin_token):
    html = test_client.get("/", headers=_admin(admin_token)).text
    tile = html.split("Services</div>", 1)[1].split("</a>", 1)[0]
    return tile


def test_the_services_tile_only_says_all_working_for_checks_that_passed(test_client, admin_token):
    assert "none set up" in _services_tile(test_client, admin_token)
    binding = _bound_service()
    assert "not checked yet" in _services_tile(test_client, admin_token)  # bound, never checked
    connector_service.update_binding(binding["id"], last_tested_at="2026-11-13T10:00:00+00:00", last_error=None)
    assert "all working" in _services_tile(test_client, admin_token)
    connector_service.update_binding(binding["id"], last_test_note="can't be tested")  # neutral: still fine
    assert "all working" in _services_tile(test_client, admin_token)
    connector_service.update_binding(binding["id"], last_error="refused")
    assert "1 failing" in _services_tile(test_client, admin_token)


def test_a_credential_an_agent_can_already_get_by_reference_is_not_listed_as_used_by_no_one(test_client, admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("cred-ws", "cred-ws", owner_id)
    agent_service.create_agent(
        agent_id="ref-reader", display_name="Ref Reader", owner_user_id=owner_id,
        read_scopes=["workspace:cred-ws"], write_scopes=[],
        capabilities=["memory", "credentials"],
    )
    credential_service.create_credential("workspace:cred-ws", "api_token", "secret-value")
    html = test_client.get("/credentials", headers=_admin(admin_token)).text
    assert "1 agent can get its reference" in html
    assert "no one yet" not in html
    assert "secret-value" not in html


def test_the_new_agent_dialog_is_not_titled_new_api_key():
    import pathlib

    source = pathlib.Path("app/routes/agents_page.py").read_text()
    assert "<h2>New API Key</h2>" not in source
    assert "rotate-modal-title" in source and "'New agent API key'" in source and "'Agent created'" in source


def test_scope_pickers_show_names_not_raw_scope_strings(test_client, admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("home-lab", "Home Lab", owner_id)
    for page in ("/credentials", "/connectors", "/memory"):
        html = test_client.get(page, headers=_admin(admin_token)).text
        assert "Home Lab" in html, page
        for raw in (">workspace:home-lab<", "(workspace:home-lab)", f">user:{owner_id}<", f"({'user:' + owner_id})"):
            assert raw not in html, (page, raw)


def test_every_page_has_a_language_a_main_landmark_and_a_skip_link(test_client, admin_token):
    for page in ("/", "/agents", "/memory", "/connectors", "/credentials", "/integrations"):
        html = test_client.get(page, headers=_admin(admin_token)).text
        assert '<html lang="en">' in html, page
        assert html.count("<main ") == 1 and 'id="main-content"' in html, page
        assert 'class="skip-link" href="#main-content"' in html, page


def test_a_credential_request_is_answered_on_the_needs_you_page(test_client, admin_token):
    from app.services import credential_request_service

    request = credential_request_service.request_credential(
        agent_id="needs-agent", user_id=None, scope="user:admin", name="DEPLOY_TOKEN", purpose="to deploy"
    )
    html = test_client.get("/", headers=_admin(admin_token)).text
    assert "<h1>Needs you</h1>" in html and "DEPLOY_TOKEN" in html
    assert f"data-credential-request='{request['request_id']}'" in html and "type='password'" in html
    r = test_client.post(
        f"/api/credentials/requests/{request['request_id']}/fulfill",
        headers=_admin(admin_token), json={"value": "s3cret-value"},
    )
    assert r.status_code == 200, r.text
    after = test_client.get("/", headers=_admin(admin_token)).text
    assert "DEPLOY_TOKEN" not in after and "Nothing needs you" in after and "s3cret-value" not in after


def test_a_task_row_is_one_line_and_its_record_opens_in_the_detail_dialog(test_client, admin_token):
    import json
    import re

    from app.services import activity_service

    task = activity_service.create_activity(agent_id="row-agent", user_id="admin", task_description="Ship the importer", memory_scope="agent:row-agent")
    activity_service.set_handoff(task["id"], {"next_steps": ["Write the docs"], "open_questions": ["Which format?"]})
    activity_service.update_activity(task["id"], status="completed", task_result="Importer shipped in commit abc123")
    html = test_client.get("/activity", headers=_admin(admin_token)).text
    row = html.split("<tr class='activity-row'", 1)[1].split("</tr>", 1)[0]
    assert "<details" not in row  # no in-cell expansion: the row stays a line or two
    assert f"data-activity-detail='{task['id']}'" in row and "class='activity-title'" in row and ">Detail</button>" in row
    assert "Importer shipped in commit abc123" not in row and "Write the docs" not in row
    assert "private to row-agent" in row and "agent:row-agent" not in row.replace("title='agent:row-agent'", "")  # plain words
    blob = re.search(r'<script type="application/json" id="activity-detail-data">(.*?)</script>', html, re.S).group(1)
    record = json.loads(blob)[task["id"]]
    assert record["result"] == "Importer shipped in commit abc123"
    assert record["next_steps"] == ["Write the docs"] and record["open_questions"] == ["Which format?"]
    assert record["needs_human"] == "" and record["waiting"] is False  # empty parts are simply absent
    assert 'id="activity-detail-modal"' in html and 'class="modal modal-detail"' in html

