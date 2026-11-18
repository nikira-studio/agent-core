"""Who may use a connection: the audience list, its endpoints, the old scope meaning kept, the per-agent switch as
exclusions, cleanup, and that being named gives no memory or credential reach."""

import json
import pathlib
import re

import pytest

from app.database import get_db
from app.security.scope_enforcer import build_agent_context
from app.services import (
    agent_service, audience_service, auth_service, connector_service, credential_service, reach_service, workspace_service,
)
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

WORK = ["memory", "coordination", "connectors_read", "connectors_execute"]


@pytest.fixture
def world(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    workspace_service.create_workspace("other", "Other place", "admin")
    agent_service.create_agent("on", "Agent on", "admin", capabilities=WORK, inherit_user_bindings=True)
    agent_service.create_agent("off", "Agent off", "admin", capabilities=WORK)  # the old default: off
    agent_service.create_agent("svc", "Service", "admin", kind="service")
    connector_service.create_connector_type(
        connector_type_id="aud-cap", display_name="Audience capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    return {"client": test_client, "headers": csrf_headers(test_client)}


def _make(scope, name="Conn", **kw):
    return connector_service.create_binding("aud-cap", name, scope, config_json='{"base_url": "http://example.test"}', **kw)


def _entries(binding_id):
    return sorted((e["kind"], e["ref"], e["exclude"]) for e in audience_service.entries_for(binding_id))


def _usable(agent_id):
    agent = agent_service.get_agent_by_id(agent_id)
    return sorted(build_agent_context(agent).usable_binding_ids or [])


# ── the old meaning of a bare scope ─────────────────────────────────────────


def test_a_bare_user_scope_seeds_exactly_as_the_upgrade_does_including_the_switch_off_agents(world):
    binding = _make("user:admin")
    assert _entries(binding["id"]) == [("agent", "off", True), ("person_agents", "admin", False)]
    assert _usable("on") == [binding["id"]] and _usable("off") == [] and _usable("svc") == []


def test_a_bare_workspace_scope_names_the_workspace(world):
    agent_service.update_agent("on", read_scopes=["workspace:acc"])
    binding = _make("workspace:acc")
    assert _entries(binding["id"]) == [("workspace", "acc", False)]
    assert _usable("on") == [binding["id"]]


def test_changing_the_scope_reseeds_but_sending_the_same_scope_keeps_a_custom_audience(world):
    client = world["client"]
    binding = _make("user:admin")
    put = client.put(f"/api/connector-bindings/{binding['id']}", json={"audience": [{"kind": "agent", "ref": "off"}]}, headers=world["headers"])
    assert put.status_code == 200, put.text
    assert _entries(binding["id"]) == [("agent", "off", False)]
    same = client.put(f"/api/connector-bindings/{binding['id']}", json={"scope": "user:admin", "name": "Renamed"}, headers=world["headers"])
    assert same.status_code == 200
    assert _entries(binding["id"]) == [("agent", "off", False)]  # an edit that re-sends the scope does not reset it
    moved = client.put(f"/api/connector-bindings/{binding['id']}", json={"scope": "workspace:acc"}, headers=world["headers"])
    assert moved.status_code == 200
    assert _entries(binding["id"]) == [("workspace", "acc", False)]  # a new owner means what it always meant


# ── the per-agent switch is exclusions ──────────────────────────────────────


def test_the_inherit_switch_adds_and_removes_the_exclusion(world):
    binding = _make("user:admin")
    assert _usable("off") == []
    agent_service.update_agent("off", inherit_user_bindings=True)
    assert _usable("off") == [binding["id"]]
    assert agent_service.get_agent_by_id("off")["inherit_user_bindings"] == 1  # the stored column follows
    agent_service.update_agent("off", inherit_user_bindings=False)
    assert _usable("off") == [] and ("agent", "off", True) in _entries(binding["id"])


def test_a_new_agent_starts_excluded_unless_the_creator_includes_it(world):
    binding = _make("user:admin")
    agent_service.create_agent("fresh", "Fresh", "admin", capabilities=WORK)
    assert _usable("fresh") == [] and ("agent", "fresh", True) in _entries(binding["id"])
    agent_service.create_agent("keen", "Keen", "admin", capabilities=WORK, inherit_user_bindings=True)
    assert _usable("keen") == [binding["id"]]
    agent_service.create_agent("worker", "A service", "admin", kind="service")
    assert not any(e[1] == "worker" for e in _entries(binding["id"]))


# ── the audience endpoints ──────────────────────────────────────────────────


def test_the_endpoints_are_admin_only_and_an_agent_never_sees_an_audience(world, test_client):
    binding = _make("workspace:acc")
    from app.services.auth_service import create_operator_token  # noqa: F401

    client = world["client"]
    ok = client.put(f"/api/access/connections/{binding['id']}/audience", json={"audience": [{"kind": "agent", "ref": "on"}]}, headers=world["headers"])
    assert ok.status_code == 200, ok.text
    assert client.get(f"/api/connector-bindings/{binding['id']}").json()["data"]["binding"]["audience"] == [{"kind": "agent", "ref": "on", "exclude": False}]
    test_client.cookies.clear()
    assert test_client.put(f"/api/access/connections/{binding['id']}/audience", json={"audience": []}).status_code in (401, 403)
    assert test_client.post("/api/access/preview/audience", json={"binding_id": binding["id"], "audience": []}).status_code in (401, 403)


def test_put_add_and_remove_are_audited_with_before_and_after(world):
    client, headers = world["client"], world["headers"]
    binding = _make("workspace:acc")
    base = f"/api/access/connections/{binding['id']}/audience"
    assert client.put(base, json={"audience": [{"kind": "workspace", "ref": "acc"}]}, headers=headers).status_code == 200
    added = client.post(base, json={"kind": "agent", "ref": "on"}, headers=headers)
    assert added.status_code == 200 and {e["ref"] for e in added.json()["data"]["audience"]} == {"acc", "on"}
    removed = client.delete(f"{base}/agent/on", headers=headers)
    assert removed.status_code == 200 and [e["ref"] for e in removed.json()["data"]["audience"]] == ["acc"]
    assert client.delete(f"{base}/agent/on", headers=headers).status_code == 404
    with get_db() as conn:
        rows = [json.loads(r["details_json"]) for r in conn.execute("SELECT details_json FROM audit_log WHERE action = 'connector_audience_changed' ORDER BY id")]
    assert [r["via"] for r in rows] == ["add", "remove"]  # the first PUT changed nothing: no row
    assert rows[0]["before"] == [{"kind": "workspace", "ref": "acc", "exclude": False}]


def test_a_service_an_unknown_name_and_a_contradiction_are_refused(world):
    client, headers = world["client"], world["headers"]
    binding = _make("workspace:acc")
    base = f"/api/access/connections/{binding['id']}/audience"
    service = client.put(base, json={"audience": [{"kind": "agent", "ref": "svc"}]}, headers=headers)
    assert service.status_code == 400 and service.json()["error"]["code"] == "INVALID_AUDIENCE" and "service" in service.json()["error"]["message"]
    assert client.put(base, json={"audience": [{"kind": "agent", "ref": "nobody"}]}, headers=headers).status_code == 400
    assert client.put(base, json={"audience": [{"kind": "workspace", "ref": "nowhere"}]}, headers=headers).status_code == 400
    both = client.put(base, json={"audience": [{"kind": "agent", "ref": "on"}, {"kind": "agent", "ref": "on", "exclude": True}]}, headers=headers)
    assert both.status_code == 400 and "both added and excluded" in both.json()["error"]["message"]
    assert client.put(base, json={"audience": [{"kind": "workspace", "ref": "acc", "exclude": True}]}, headers=headers).status_code == 400
    assert _entries(binding["id"]) == [("workspace", "acc", False)]  # nothing was written


def test_an_exclusion_beats_every_other_entry(world):
    agent_service.update_agent("on", read_scopes=["workspace:acc"])
    binding = _make("workspace:acc")
    with get_db() as conn:
        audience_service.replace(conn, binding["id"], [
            {"kind": "workspace", "ref": "acc", "exclude": False},
            {"kind": "person_agents", "ref": "admin", "exclude": False},
            {"kind": "agent", "ref": "on", "exclude": True},
        ])
        conn.commit()
    assert _usable("on") == []


def test_the_preview_lists_who_gains_and_who_loses_and_writes_nothing(world):
    agent_service.update_agent("on", read_scopes=["workspace:acc"])
    binding = _make("workspace:acc")
    before = _entries(binding["id"])
    preview = world["client"].post(
        "/api/access/preview/audience",
        json={"binding_id": binding["id"], "audience": [{"kind": "agent", "ref": "off"}]},
        headers=world["headers"],
    ).json()["data"]
    assert [g["agent_id"] for g in preview["gain"]] == ["off"] and [l["agent_id"] for l in preview["lose"]] == ["on"]
    assert _entries(binding["id"]) == before


# ── being named gives use and nothing else ──────────────────────────────────


def test_a_named_agent_may_use_the_connection_but_gains_no_memory_or_credential_reach(world):
    credential = credential_service.create_credential("workspace:other", "other-key", "not-shown")
    binding = _make("workspace:other", credential_id=credential["id"])
    agent = agent_service.get_agent_by_id("off")
    before_ctx = build_agent_context(agent)
    before_summary = reach_service.agent_summary(agent)
    with get_db() as conn:
        audience_service.replace(conn, binding["id"], [{"kind": "agent", "ref": "off", "exclude": False}])
        conn.commit()
    after_ctx = build_agent_context(agent)
    assert binding["id"] in after_ctx.usable_binding_ids and binding["id"] not in (before_ctx.usable_binding_ids or [])
    assert after_ctx.read_scopes == before_ctx.read_scopes and after_ctx.write_scopes == before_ctx.write_scopes
    after_summary = reach_service.agent_summary(agent)
    assert after_summary["credentials"] == before_summary["credentials"] == []  # it cannot reference the connection's credential
    assert [c["name"] for c in after_summary["connections"]] == ["Conn"]


# ── no row is left behind ───────────────────────────────────────────────────


def test_deleting_an_agent_a_workspace_or_a_person_removes_their_audience_rows(world):
    create_user("pat", "pat@test.local", "testpassword123", "Pat", "user")
    agent_service.create_agent("pats", "Pat's", "pat", capabilities=WORK)
    binding = _make("workspace:acc")
    with get_db() as conn:
        audience_service.replace(conn, binding["id"], [
            {"kind": "agent", "ref": "on", "exclude": False}, {"kind": "agent", "ref": "off", "exclude": True},
            {"kind": "workspace", "ref": "other", "exclude": False}, {"kind": "person_agents", "ref": "pat", "exclude": False},
        ])
        conn.commit()
    assert agent_service.delete_agent_hard("on")
    assert workspace_service.delete_workspace_hard("other")
    assert _entries(binding["id"]) == [("agent", "off", True), ("person_agents", "pat", False)]
    ok, _ = auth_service.delete_user("pat")
    assert ok and _entries(binding["id"]) == [("agent", "off", True)]
    assert agent_service.delete_agent_hard("off") and _entries(binding["id"]) == []


def test_deleting_a_connection_removes_its_audience_and_a_retired_agent_shows_under_left_behind(world):
    keep, gone = _make("workspace:acc", "Keep"), _make("workspace:acc", "Gone")
    with get_db() as conn:
        audience_service.replace(conn, keep["id"], [{"kind": "agent", "ref": "off", "exclude": False}])
        audience_service.replace(conn, gone["id"], [{"kind": "agent", "ref": "off", "exclude": False}])
        conn.commit()
    connector_service.delete_binding(gone["id"])
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connector_binding_audience WHERE binding_id = ?", (gone["id"],)).fetchone()[0] == 0
    agent_service.deactivate_agent("off")
    left = world["client"].get("/api/access/overview").json()["data"]["unused"]["left_behind"]
    assert [r["name"] for r in left] == ["Agent off"] and "Keep" in left[0]["why"]


# ── the use checks all go through the audience ──────────────────────────────


def test_no_use_check_reads_a_connections_scope_directly():
    """A use or visibility check that still compares a connection's scope with an agent's scopes would skip the audience.
    Only manage checks (can_write on the owner scope) and the frozen 1.3 rule may read it."""
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(r'can_read\((?:binding|b|wanted_binding|binding_for_auth)\["scope"\]\)|\.can\("connector", "read", scope=')
    allowed = {"access_check/legacy_v13.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) and "can_use_binding" not in line:
                offenders.append(f"{rel}:{number}: {line.strip()}")
    # Allowed: the person-level read inside can_use_binding itself (a signed-in person is not an agent) and the two
    # dashboard pages that filter for a signed-in person.
    assert all(o.startswith(("routes/overview_page.py", "routes/connectors_page.py", "security/effective_authority.py")) for o in offenders), offenders


def test_an_agent_cannot_choose_an_audience_and_its_connection_gets_the_scope_meaning(test_client, admin_token, agent_token):
    """Only an administrator sets who may use a connection; an agent that creates one gets what its scope always meant."""
    connector_service.create_connector_type(
        connector_type_id="aud-cap2", display_name="Audience capability 2", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    body = {"connector_type_id": "aud-cap2", "name": "Mine", "scope": "agent:testagent", "config_json": '{"base_url": "http://example.test"}'}
    headers = {"Authorization": f"Bearer {agent_token}"}
    refused = test_client.post("/api/connector-bindings", headers=headers, json={**body, "audience": [{"kind": "agent", "ref": "testagent"}]})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "FORBIDDEN"
    made = test_client.post("/api/connector-bindings", headers=headers, json=body)
    assert made.status_code == 201, made.text
    assert "audience" not in made.json()["data"]["binding"]  # an agent's answers never carry an audience
    assert _entries(made.json()["data"]["binding"]["id"]) == [("agent", "testagent", False)]
