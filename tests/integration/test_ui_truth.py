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
    tile = html.split("Capabilities</div>", 1)[1].split("</a>", 1)[0]
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


def _used_by(html, credential_name):
    row = html.split(credential_name.replace("_", "_<wbr>"), 1)[1].split("</tr>", 1)[0]  # the page lets long names wrap
    return row.split("data-label='Used by'>", 1)[1].split("</td>", 1)[0]


def _ref_world():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("cred-ws", "cred-ws", owner_id)
    for agent_id, name, caps in (
        ("ref-reader", "Ref Reader", ["memory", "credentials"]),
        ("second-reader", "Second Reader", ["memory", "credentials"]),
        ("holder", "Holder", ["memory", "credentials", "credentials_resolve"]),
    ):
        agent_service.create_agent(
            agent_id=agent_id, display_name=name, owner_user_id=owner_id,
            read_scopes=["workspace:cred-ws"], write_scopes=[], capabilities=caps,
        )
    return owner_id


def test_the_used_by_cell_always_says_who_was_given_the_secret_and_who_else_can_see_its_name(test_client, admin_token):
    _ref_world()
    credential_service.create_credential("workspace:cred-ws", "api_token", "secret-value")
    html = test_client.get("/credentials", headers=_admin(admin_token)).text
    cell = _used_by(html, "api_token")
    # nobody was given it, three agents can see its name: both facts are stated, in the same two lines
    assert "Given the secret:" in cell and "no one" in cell
    assert "3 others can see its name" in cell
    assert "Ref Reader" in cell.split("title='", 1)[1].split("'", 1)[0]  # the names are on hover
    assert "use it wherever Agent Core makes a connection" in cell and "does not show them the secret" in cell
    assert "secret-value" not in html and "can get its reference" not in html


def test_a_grant_adds_a_chip_and_keeps_the_other_readers_fact(test_client, admin_token):
    _ref_world()
    entry = credential_service.create_credential("workspace:cred-ws", "api_token", "secret-value")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO credential_consumer_grants (id, entry_id, consumer_agent_id, created_by, created_at) "
            "VALUES ('g1', ?, 'holder', 'admin', '2026-11-14T00:00:00+00:00')", (entry["id"],),
        )
    cell = _used_by(test_client.get("/credentials", headers=_admin(admin_token)).text, "api_token")
    assert "Given the secret:" in cell and ">Holder<" in cell and "no one" not in cell
    assert "2 others can see its name" in cell  # the holder is not counted twice


def test_one_other_is_singular_and_nobody_else_leaves_no_line(test_client, admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("cred-ws", "cred-ws", owner_id)
    agent_service.create_agent(
        agent_id="only-reader", display_name="Only Reader", owner_user_id=owner_id,
        read_scopes=["workspace:cred-ws"], write_scopes=[], capabilities=["memory", "credentials"],
    )
    credential_service.create_credential("workspace:cred-ws", "api_token", "secret-value")
    cell = _used_by(test_client.get("/credentials", headers=_admin(admin_token)).text, "api_token")
    assert "1 other can see its name" in cell
    credential_service.create_credential("workspace:nobody", "lonely_token", "secret-value")
    lonely = _used_by(test_client.get("/credentials", headers=_admin(admin_token)).text, "lonely_token")
    assert "Given the secret:" in lonely and "no one" in lonely and "can see its name" not in lonely


def test_the_dialog_uses_the_same_two_labels_as_the_list(test_client, admin_token):
    html = test_client.get("/credentials", headers=_admin(admin_token)).text
    assert ">Given the secret</h3>" in html and ">Others who can see its name</h3>" in html
    assert "Agents with access" not in html
    js = test_client.get("/static/js/credentials.js").text
    assert "renderUsedBy" in js and "Given the secret:" in js and "location.reload" not in js.split("function closeAccess", 1)[1].split("}", 1)[0]


def test_the_new_agent_dialog_is_not_titled_new_api_key():
    import pathlib

    source = pathlib.Path("app/routes/agents_page.py").read_text()
    assert "<h2>New API Key</h2>" not in source
    assert "rotate-modal-title" in source and "'Agent created'" in source
    assert "'New agent API key'" not in source  # a key is added from the Keys dialog, titled for the agent


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

