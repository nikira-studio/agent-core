"""One answer to "who can reach what": the local credential helper, the access pages' data, the previews, the grant
rule for services, and the upgrade check's view of the helper change."""

import json
import sqlite3

import pytest

from app.database import get_db
from app.services import agent_service, broker_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

SCOPE = "workspace:acc"


@pytest.fixture
def world(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    workspace_service.create_workspace("other", "Other place", "admin")
    work = ["memory", "coordination", "connectors_read", "connectors_execute"]
    agent_service.create_agent("asst", "Assistant", "admin", capabilities=[*work, "credentials"], read_scopes=[SCOPE], write_scopes=[])
    agent_service.create_agent("plain", "Plain", "admin", capabilities=work, read_scopes=[SCOPE], write_scopes=[])
    agent_service.create_agent("svc", "Service", "admin", kind="service")
    entry = credential_service.create_credential(SCOPE, "shared-key", "not-shown")
    connector_service.create_connector_type(
        connector_type_id="reach-cap", display_name="Reach capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    binding = connector_service.create_binding(
        "reach-cap", "Reach connection", SCOPE, credential_id=entry["id"], config_json='{"base_url": "http://example.test"}'
    )
    return {"client": test_client, "entry": entry, "binding": binding, "headers": csrf_headers(test_client)}


def _helper(client, entry, agent_id):
    broker_service.ensure_broker_credential()
    plaintext = broker_service._read_broker_credential()
    return client.post(
        "/internal/credentials/resolve",
        headers={"Authorization": f"Broker {plaintext}"},
        json={"variable_name": entry["reference_name"], "agent_id": agent_id},
    )


def _denials(agent_id):
    with get_db() as conn:
        return [
            json.loads(r["details_json"])["reason"]
            for r in conn.execute(
                "SELECT details_json FROM audit_log WHERE action = 'broker_resolve_denied' AND actor_id = ? ORDER BY id", (agent_id,)
            )
        ]


# ── the helper applies the same rule as MCP credential_get ──────────────────


def test_the_helper_serves_an_agent_with_the_permission_and_the_scope(world):
    r = _helper(world["client"], world["entry"], "asst")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["value"] == "not-shown"


def test_the_helper_refuses_an_agent_without_the_reference_permission_and_says_so_in_the_audit_log(world):
    r = _helper(world["client"], world["entry"], "plain")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_REQUIRED"
    assert "not-shown" not in r.text
    assert _denials("plain") == ["needs_permission"]


def test_the_helper_refuses_a_service_and_points_to_grants(world):
    r = _helper(world["client"], world["entry"], "svc")
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_AN_AGENT"
    assert _denials("svc") == ["not_an_agent"]


def test_the_helper_still_refuses_a_scope_the_agent_does_not_reach(world):
    other = credential_service.create_credential("workspace:other", "elsewhere", "not-shown")
    r = _helper(world["client"], other, "asst")
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCOPE_DENIED"
    assert _denials("asst") == ["scope_denied"]


# ── the upgrade check lists exactly the removed helper reach ────────────────


def test_the_comparison_across_the_helper_change_lists_only_removed_helper_reach(world):
    from app.access_check import compare, snapshot

    conn = sqlite3.connect(str(__import__("app.database", fromlist=["get_db_path"]).get_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        before = snapshot.capture(conn, helper_rule="1.3")
        after = snapshot.capture(conn)
    finally:
        conn.close()
    diffs = compare.differences(before, after)
    assert {d["path"][0] for d in diffs} == {"helper_fetch"}
    assert all(d["change"] in ("changed", "removed") for d in diffs)
    # plain (no permission) and svc (a service) lose the cells they had; asst keeps its
    lost = {d["path"][1] for d in diffs}
    assert "plain" in lost and "asst" not in lost
    after_cells = after["helper_fetch"]
    assert after_cells["asst"] == after["mcp_reference"]["asst"]
    assert after_cells["plain"] == [] and after_cells["svc"] == []


# ── the pages' data ────────────────────────────────────────────────────────


def test_the_credential_page_groups_connections_agents_and_services(world):
    client = world["client"]
    granted = client.post(
        "/api/credential-consumers/grants",
        json={"consumer_agent_id": "svc", "entry_id": world["entry"]["id"], "allow_write": False},
        headers=world["headers"],
    )
    assert granted.status_code == 201, granted.text
    data = client.get(f"/api/access/credentials/{world['entry']['id']}").json()["data"]
    assert [c["name"] for c in data["connections"]] == ["Reach connection"]
    by_agent = {a["agent_id"]: a for a in data["agents"]}
    assert set(by_agent) == {"asst", "plain"}
    assert by_agent["asst"]["status"] == "can_reference" and by_agent["asst"]["reason"] == "through workspace Accounts"
    assert by_agent["plain"]["status"] == "needs_permission" and by_agent["plain"]["needs_label"] == "Reference credentials"
    assert [s["service_id"] for s in data["services"]] == ["svc"]
    assert data["services_to_add"] == []
    assert "not-shown" not in json.dumps(data)


def test_access_views_are_for_an_admin_session_only(world, test_client):
    from tests.conftest import operator_token_for  # noqa: F401

    test_client.cookies.clear()
    r = test_client.get(f"/api/access/credentials/{world['entry']['id']}")
    assert r.status_code in (401, 403)


def test_the_agent_view_shows_effective_access_and_a_service_has_none(world):
    client = world["client"]
    data = client.get("/api/access/agents/plain").json()["data"]
    assert [w["id"] for w in data["workspaces"]] == ["acc"]
    assert [c["name"] for c in data["connections"]] == ["Reach connection"]
    assert [c["status"] for c in data["credentials"]] == ["needs_permission"]
    assert client.get("/api/access/agents/svc").status_code == 404
    svc = client.get("/api/access/services/svc").json()["data"]
    assert svc["grants"] == [] and "keys" in svc
    assert client.get("/api/access/services/asst").status_code == 404


def test_the_connection_view_says_who_can_use_it(world):
    data = world["client"].get(f"/api/access/connections/{world['binding']['id']}").json()["data"]
    assert {u["agent_id"] for u in data["users"]} == {"asst", "plain"}
    assert all(u["status"] == "can_use" for u in data["users"])


# ── previews ────────────────────────────────────────────────────────────────


def test_changing_a_credentials_owner_previews_who_gains_and_who_loses(world):
    preview = world["client"].post(
        "/api/access/preview/credential-owner",
        json={"entry_id": world["entry"]["id"], "scope": "agent:asst"},
        headers=world["headers"],
    ).json()["data"]
    # asst owns agent:asst, so it reaches the credential either way; plain reached it only through the workspace
    assert [a["agent_id"] for a in preview["lose"]] == ["plain"]
    assert preview["gain"] == [] and preview["unchanged"] == 1
    to_workspace = world["client"].post(
        "/api/access/preview/credential-owner",
        json={"entry_id": world["entry"]["id"], "scope": "workspace:other"},
        headers=world["headers"],
    ).json()["data"]
    assert {a["agent_id"] for a in to_workspace["lose"]} == {"asst", "plain"}


def test_adding_a_workspace_previews_memory_credentials_and_connections(world):
    other = credential_service.create_credential("workspace:other", "other-key", "not-shown")
    connector_service.create_binding(
        "reach-cap", "Other connection", "workspace:other", config_json='{"base_url": "http://example.test"}'
    )
    preview = world["client"].post(
        "/api/access/preview/agent-workspace",
        json={"agent_id": "plain", "workspace_id": "other", "action": "add"},
        headers=world["headers"],
    ).json()["data"]
    assert preview["opens_memory"] is True and preview["no_effect"] is False
    assert [c["name"] for c in preview["credentials"]] == ["other-key"]
    assert preview["credentials"][0]["status"] == "needs_permission"  # plain lacks Reference credentials
    assert [c["name"] for c in preview["connections"]] == ["Other connection"]
    assert preview["connections"][0]["status"] == "can_use"
    assert other["id"]  # the value is never part of the answer
    assert "not-shown" not in json.dumps(preview)


def test_a_workspace_the_person_is_not_in_previews_as_having_no_effect(world):
    create_user("pat", "pat@test.local", "testpassword123", "Pat", "user")
    agent_service.create_agent("pats", "Pat's agent", "pat", capabilities=["memory"], read_scopes=[], write_scopes=[])
    preview = world["client"].post(
        "/api/access/preview/agent-workspace",
        json={"agent_id": "pats", "workspace_id": "acc", "action": "add"},
        headers=world["headers"],
    ).json()["data"]
    assert preview["opens_memory"] is False and preview["no_effect"] is True


# ── turning a permission on is one recorded action ──────────────────────────


def test_turning_a_permission_on_adds_it_records_it_and_never_removes(world):
    client = world["client"]
    r = client.post("/api/agents/plain/permissions/credentials/turn-on", headers=world["headers"])
    assert r.status_code == 200, r.text
    assert "credentials" in r.json()["data"]["permissions"] and "memory" in r.json()["data"]["permissions"]
    with get_db() as conn:
        row = conn.execute("SELECT details_json FROM audit_log WHERE action = 'agent_permission_turned_on'").fetchone()
    assert json.loads(row["details_json"])["permission"] == "credentials"
    again = client.post("/api/agents/plain/permissions/credentials/turn-on", headers=world["headers"])
    assert again.status_code == 200
    assert client.get(f"/api/access/credentials/{world['entry']['id']}").json()["data"]["agents"][1]["status"] == "can_reference"


def test_turning_on_an_unknown_permission_or_one_for_a_service_is_refused(world):
    client = world["client"]
    assert client.post("/api/agents/plain/permissions/teleport/turn-on", headers=world["headers"]).status_code == 400
    refused = client.post("/api/agents/svc/permissions/memory/turn-on", headers=world["headers"])
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "SERVICE_RULE"
    assert agent_service.parse_capabilities(agent_service.get_agent_by_id("svc")["capabilities_json"]) == ["credentials_resolve"]


# ── a secret's value is given to a service ──────────────────────────────────


def test_a_new_grant_goes_to_a_service_and_an_older_agent_held_grant_is_listed(world):
    client = world["client"]
    refused = client.post(
        "/api/credential-consumers/grants",
        json={"consumer_agent_id": "plain", "entry_id": world["entry"]["id"], "allow_write": False},
        headers=world["headers"],
    )
    assert refused.status_code == 403
    # a grant that an entry which is not a service already held (from before the rule) is honoured and listed
    agent_service.update_agent("plain", capabilities=["memory", "credentials_resolve"])
    with get_db() as conn:
        conn.execute(
            "INSERT INTO credential_consumer_grants (id, consumer_agent_id, entry_id, created_by, created_at) VALUES ('g-old', 'plain', ?, 'admin', '2026-01-01')",
            (world["entry"]["id"],),
        )
        conn.commit()
    data = client.get(f"/api/access/credentials/{world['entry']['id']}").json()["data"]
    assert data["held_by_agents"] == ["Plain"]


def test_a_connection_can_have_its_credential_taken_away_and_given_back_and_both_are_audited(world):
    client, binding, entry = world["client"], world["binding"], world["entry"]
    taken = client.put(f"/api/connector-bindings/{binding['id']}", json={"reset_credential": True}, headers=world["headers"])
    assert taken.status_code == 200, taken.text
    assert taken.json()["data"]["binding"]["credential_id"] is None
    assert client.get(f"/api/access/credentials/{entry['id']}").json()["data"]["connections"] == []
    back = client.put(f"/api/connector-bindings/{binding['id']}", json={"credential_id": entry["id"]}, headers=world["headers"])
    assert back.status_code == 200 and back.json()["data"]["binding"]["credential_id"] == entry["id"]
    with get_db() as conn:
        rows = conn.execute("SELECT details_json FROM audit_log WHERE action = 'connector_binding_updated' ORDER BY id").fetchall()
    details = [json.loads(r["details_json"]) for r in rows]
    assert details[0] == {"entry_before": entry["id"], "entry_after": None}
    assert details[1] == {"entry_before": None, "entry_after": entry["id"]}


def test_the_helpers_reach_is_the_mcp_reach_for_every_entry(world):
    """One rule, two doors: for every agent the snapshot's helper cells equal its MCP reference cells, and a service has none."""
    from app.access_check import snapshot
    from app.database import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        cells = snapshot.capture(conn)
    finally:
        conn.close()
    for entry_id, identity in cells["identities"].items():
        if not identity["active"]:
            continue
        if identity["kind"] == "service":
            assert cells["helper_fetch"][entry_id] == [], entry_id
        else:
            assert cells["helper_fetch"][entry_id] == cells["mcp_reference"][entry_id], entry_id
    assert cells["helper_fetch"]["asst"] != []  # the check is not vacuous


def test_the_frozen_1_3_use_rule_gives_the_same_matrices_as_the_current_one_until_the_audience_list_exists(world):
    """legacy_v13 is the "before" of the step that adds the audience list; it must say what the code says today."""
    from app.access_check import snapshot
    from app.database import get_db_path

    # an agent acting for a person who owns a user-scope connection, with the old switch off and on
    connector_service.create_binding("reach-cap", "Personal", "user:admin", config_json='{"base_url": "http://example.test"}')
    agent_service.update_agent("plain", inherit_user_bindings=True)
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        now = snapshot.capture(conn)
        frozen = snapshot.capture(conn, use_rule="1.3")
    finally:
        conn.close()
    assert now["see"] == frozen["see"] and now["run"] == frozen["run"]
    assert any(frozen["see"][a] for a in frozen["see"])  # not vacuous
