"""Showing a stored value again: only a person at the dashboard, only a credential they can read, recorded in the audit
log without the value, and never in the page until they ask."""

import json

import pytest

from app.database import get_db
from app.security.rate_limiter import RL
from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_operator_token, create_session, create_user
from tests.conftest import csrf_headers

VALUE = "j-reveal-secret-value-123"


@pytest.fixture
def world(test_client, clean_db):
    RL.reset()
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("pat", "pat@test.local", "testpassword123", "Pat", "user")
    workspace_service.create_workspace("shown", "Shown Place", "admin")
    agent, key = agent_service.create_agent(
        "asst", "Assistant", "admin", capabilities=["memory", "credentials"], read_scopes=["workspace:shown"], write_scopes=[]
    )
    entry = credential_service.create_credential("workspace:shown", "shown-key", VALUE)
    mine = credential_service.create_credential("user:pat", "pats-key", "pats-secret-value")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return {"client": test_client, "headers": csrf_headers(test_client), "entry": entry, "mine": mine, "agent_key": key}


def _reveal(world, entry=None, **kwargs):
    entry = entry or world["entry"]
    return world["client"].post(f"/api/credentials/entries/{entry['id']}/reveal", headers=kwargs.pop("headers", world["headers"]), **kwargs)


def _audit_rows(entry_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = 'credential_reveal' AND resource_id = ?", (entry_id,)).fetchall()]


def test_a_person_at_the_dashboard_sees_the_value_and_it_is_not_cached(world):
    r = _reveal(world)
    assert r.status_code == 200 and r.json()["data"]["value"] == VALUE
    assert "no-store" in r.headers["cache-control"]


def test_the_audit_row_names_the_person_and_the_credential_and_holds_no_value(world):
    _reveal(world)
    rows = _audit_rows(world["entry"]["id"])
    assert len(rows) == 1
    row = rows[0]
    assert (row["actor_type"], row["actor_id"], row["resource_type"], row["result"]) == ("user", "admin", "credential", "success")
    assert json.loads(row["details_json"]) == {"name": "shown-key", "via": "dashboard"}
    assert VALUE not in json.dumps(row)


def test_an_agent_key_is_refused_and_leaves_no_reveal_row(world, test_client):
    r = test_client.post(f"/api/credentials/entries/{world['entry']['id']}/reveal",
                         headers={"Authorization": f"Bearer {world['agent_key']}"})
    assert r.status_code == 403
    assert VALUE not in r.text
    assert _audit_rows(world["entry"]["id"]) == []


def test_an_operator_token_is_a_person_acting_and_the_audit_row_says_so(world, test_client):
    test_client.cookies.clear()
    token, _ = create_operator_token("admin", 30)
    r = test_client.post(f"/api/credentials/entries/{world['entry']['id']}/reveal", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["data"]["value"] == VALUE
    row = _audit_rows(world["entry"]["id"])[0]
    assert (row["actor_type"], row["actor_id"]) == ("user", "admin")
    assert json.loads(row["details_json"]) == {"name": "shown-key", "via": "operator token"}


def test_a_person_who_cannot_read_the_owner_is_refused_and_one_who_can_is_not(world, test_client):
    test_client.cookies.set("session_token", create_session("pat", channel="dashboard")["session_id"])
    headers = csrf_headers(test_client)
    assert _reveal(world, headers=headers).status_code == 403  # a workspace pat is not in
    mine = _reveal(world, entry=world["mine"], headers=headers)
    assert mine.status_code == 200 and mine.json()["data"]["value"] == "pats-secret-value"


def test_an_expired_credential_says_so_and_shows_nothing(world):
    entry = credential_service.create_credential("workspace:shown", "old-key", "old-value", expires_at="2020-01-01T00:00:00+00:00")
    r = _reveal(world, entry=entry)
    assert r.status_code == 410 and "old-value" not in r.text


def test_a_connection_owned_credential_follows_the_person_rule(world, test_client):
    connector_service.create_connector_type(
        connector_type_id="rv-cap", display_name="Reveal capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    made = test_client.post("/api/connector-bindings", headers=world["headers"], json={
        "connector_type_id": "rv-cap", "name": "Main", "scope": "user:admin", "config_json": '{"base_url": "http://example.test"}',
        "new_credential": {"name": "its-key", "value": "connection-value", "owned_by": "connection"},
    })
    assert made.status_code == 201, made.text
    binding = made.json()["data"]["binding"]
    owned = credential_service.get_credential(binding["credential_id"])
    assert owned["scope"] == f"capability:{binding['id']}"
    shown = _reveal(world, entry=owned)
    assert shown.status_code == 200 and shown.json()["data"]["value"] == "connection-value"
    # an agent never reads it, even with a key
    agent = test_client.post(f"/api/credentials/entries/{owned['id']}/reveal", headers={"Authorization": f"Bearer {world['agent_key']}"})
    assert agent.status_code == 403
    # a person who may not manage the connection does not either
    test_client.cookies.set("session_token", create_session("pat", channel="dashboard")["session_id"])
    assert _reveal(world, entry=owned, headers=csrf_headers(test_client)).status_code == 403


def test_showing_values_is_rate_limited(world):
    codes = [_reveal(world).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and 429 in codes[30:]


def test_the_page_never_carries_a_value_and_says_what_showing_it_means(world):
    html = world["client"].get("/credentials").text
    assert VALUE not in html and "pats-secret-value" not in html
    assert "can show its value again from Edit" in html and "recorded in the audit log" in html
    assert "never shown again" not in html
    assert 'id="edit-credential-reveal"' in html and 'id="edit-credential-revealed" hidden' in html
    # the table has no reveal button: it is in the edit dialog only
    table = html[html.index("<table"):html.index("</table>")]
    assert "reveal" not in table.lower() and "Show value" not in table


def test_the_script_asks_only_on_a_click_and_puts_the_value_in_with_textcontent(world):
    source = world["client"].get("/static/js/credentials.js").text
    start = source.index("// ── showing a stored value")
    block = source[start:source.index("async function editCredential")]
    assert "/reveal" in block and "textContent" in block and "innerHTML" not in block
    assert "REVEAL_SECONDS = 30" in block and "visibilitychange" in block and "MutationObserver" in block
    assert "localStorage" not in block and "sessionStorage" not in block and "location" not in block
