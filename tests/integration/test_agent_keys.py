"""An agent can hold several keys, each added and revoked on its own."""

import hashlib

import pytest

from app.database import get_db
from app.services import agent_service
from tests.conftest import operator_token_for


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def alive(client, key):
    """Whether the key authenticates as an agent (a memory search needs only a valid, active agent key)."""
    return client.post("/api/memory/search", headers=auth(key), json={"query": "anything"}).status_code == 200


@pytest.fixture
def agent(test_client, admin_token):
    r = test_client.post("/api/agents", headers=auth(admin_token), json={"id": "multi", "display_name": "Multi"})
    assert r.status_code == 201, r.text
    return "multi"


def add(client, token, agent_id, label="laptop"):
    r = client.post(f"/api/agents/{agent_id}/keys", headers=auth(token), json={"label": label})
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["api_key"].startswith("ac_sk_")
    return data["key"], data["api_key"]


def test_a_new_agent_has_no_key_until_one_is_added(test_client, admin_token, agent):
    assert test_client.get(f"/api/agents/{agent}/keys", headers=auth(admin_token)).json()["data"]["keys"] == []
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_keys WHERE agent_id=?", (agent,)).fetchone()[0] == 0


def test_two_keys_work_side_by_side_and_one_can_be_revoked_alone(test_client, admin_token, agent):
    first, first_key = add(test_client, admin_token, agent, "laptop")
    second, second_key = add(test_client, admin_token, agent, "ci runner")
    assert alive(test_client, first_key) and alive(test_client, second_key)
    r = test_client.delete(f"/api/agents/{agent}/keys/{first['id']}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert not alive(test_client, first_key)
    assert alive(test_client, second_key)
    # revoking twice is a plain not-found, not a second success
    assert test_client.delete(f"/api/agents/{agent}/keys/{first['id']}", headers=auth(admin_token)).status_code == 404


def test_the_list_shows_labels_and_use_but_never_a_key_or_hash(test_client, admin_token, agent):
    record, key = add(test_client, admin_token, agent, "laptop")
    assert alive(test_client, key)
    r = test_client.get(f"/api/agents/{agent}/keys", headers=auth(admin_token))
    text = r.text
    assert key not in text and hashlib.sha256(key.encode()).hexdigest() not in text
    (row,) = r.json()["data"]["keys"]
    assert row["label"] == "laptop" and row["key_hint"] == key[-4:]
    assert row["last_used_at"] and row["created_by"] == "admin" and row["revoked_at"] is None
    assert set(row) == {"id", "label", "key_hint", "created_at", "created_by", "last_used_at", "revoked_at", "revoked_by"}


def test_revoked_keys_are_hidden_unless_asked_for(test_client, admin_token, agent):
    record, _ = add(test_client, admin_token, agent)
    test_client.delete(f"/api/agents/{agent}/keys/{record['id']}", headers=auth(admin_token))
    assert test_client.get(f"/api/agents/{agent}/keys", headers=auth(admin_token)).json()["data"]["keys"] == []
    shown = test_client.get(f"/api/agents/{agent}/keys?include_revoked=true", headers=auth(admin_token))
    (row,) = shown.json()["data"]["keys"]
    assert row["revoked_at"] and row["revoked_by"] == "admin"


def test_a_label_is_required_and_trimmed(test_client, admin_token, agent):
    for bad in ("", "   "):
        r = test_client.post(f"/api/agents/{agent}/keys", headers=auth(admin_token), json={"label": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_LABEL"
    record, _ = add(test_client, admin_token, agent, "  " + "x" * 200)
    assert record["label"] == "x" * 60
    assert test_client.post(f"/api/agents/{agent}/keys", headers=auth(admin_token), json={}).status_code == 422


def test_an_agent_holds_at_most_twenty_live_keys(test_client, admin_token, agent):
    ids = [add(test_client, admin_token, agent, f"k{i}")[0]["id"] for i in range(20)]
    r = test_client.post(f"/api/agents/{agent}/keys", headers=auth(admin_token), json={"label": "one too many"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "TOO_MANY_KEYS"
    test_client.delete(f"/api/agents/{agent}/keys/{ids[0]}", headers=auth(admin_token))
    add(test_client, admin_token, agent, "fits now")


def test_rotate_key_revokes_every_live_key_and_issues_one(test_client, admin_token, agent):
    _, a = add(test_client, admin_token, agent, "a")
    _, b = add(test_client, admin_token, agent, "b")
    r = test_client.post(f"/api/agents/{agent}/rotate_key", headers=auth(admin_token))
    assert r.status_code == 200
    new = r.json()["data"]["api_key"]
    assert not alive(test_client, a) and not alive(test_client, b) and alive(test_client, new)
    assert len(agent_service.list_agent_keys(agent)) == 1


def test_only_a_person_who_manages_the_agent_can_touch_its_keys(test_client, admin_token, agent):
    from app.services.auth_service import create_user

    create_user("other", "other@test.local", "testpassword123", "Other", "user")
    other = operator_token_for("other")
    record, key = add(test_client, admin_token, agent)
    assert test_client.get(f"/api/agents/{agent}/keys", headers=auth(other)).status_code == 403
    assert test_client.post(f"/api/agents/{agent}/keys", headers=auth(other), json={"label": "x"}).status_code == 403
    assert test_client.delete(f"/api/agents/{agent}/keys/{record['id']}", headers=auth(other)).status_code == 403
    assert alive(test_client, key)
    # the owner of an agent manages its keys
    r = test_client.post("/api/agents", headers=auth(other), json={
        "id": "owned", "display_name": "Owned", "read_scopes": ["user:other"], "write_scopes": ["user:other"]})
    assert r.status_code == 201, r.text
    add(test_client, other, "owned")
    # a missing agent is a 404
    assert test_client.get("/api/agents/nobody/keys", headers=auth(admin_token)).status_code == 404


def test_an_agent_cannot_mint_or_revoke_keys_with_its_own_key(test_client, admin_token, agent):
    record, key = add(test_client, admin_token, agent)
    other, other_key = add(test_client, admin_token, agent, "other")
    for method, path, body in [
        ("get", f"/api/agents/{agent}/keys", None),
        ("post", f"/api/agents/{agent}/keys", {"label": "sneaky"}),
        ("delete", f"/api/agents/{agent}/keys/{other['id']}", None),
        ("post", f"/api/agents/{agent}/rotate_key", None),
    ]:
        r = test_client.request(method, path, headers=auth(key), json=body)
        assert r.status_code in (401, 403), (path, r.status_code)
    assert alive(test_client, other_key)


def test_an_inactive_agent_gets_no_key_and_its_keys_stop_and_return_with_it(test_client, admin_token, agent):
    _, key = add(test_client, admin_token, agent)
    test_client.delete(f"/api/agents/{agent}", headers=auth(admin_token))
    assert not alive(test_client, key)
    r = test_client.post(f"/api/agents/{agent}/keys", headers=auth(admin_token), json={"label": "late"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "AGENT_INACTIVE"
    test_client.post(f"/api/agents/{agent}/activate", headers=auth(admin_token))
    assert alive(test_client, key)


def test_deleting_an_agent_removes_its_keys(test_client, admin_token, agent):
    _, key = add(test_client, admin_token, agent)
    assert test_client.post(f"/api/agents/{agent}/purge", headers=auth(admin_token)).status_code == 200
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_keys WHERE agent_id=?", (agent,)).fetchone()[0] == 0
    assert not alive(test_client, key)


def test_the_service_issues_a_first_key_by_default_and_none_on_request(clean_db):
    owner = "keyowner"
    from app.services.auth_service import create_user

    create_user(owner, "keyowner@test.local", "testpassword123", "Key Owner", "user")
    _, key = agent_service.create_agent("svc-a", "A", owner)
    assert agent_service.get_agent_by_api_key(key)["id"] == "svc-a"
    assert [k["label"] for k in agent_service.list_agent_keys("svc-a")] == ["First key"]
    _, none = agent_service.create_agent("svc-b", "B", owner, issue_key=False)
    assert none == "" and agent_service.list_agent_keys("svc-b") == []


def test_the_old_column_mirrors_the_newest_live_key_so_an_older_build_never_accepts_a_revoked_one(test_client, admin_token, agent):
    def legacy():
        return agent_service.get_agent_by_id(agent) and _legacy_hash(agent)

    first, first_key = add(test_client, admin_token, agent, "one")
    assert _legacy_hash(agent) == hashlib.sha256(first_key.encode()).hexdigest()
    second, second_key = add(test_client, admin_token, agent, "two")
    assert _legacy_hash(agent) == hashlib.sha256(second_key.encode()).hexdigest()
    test_client.delete(f"/api/agents/{agent}/keys/{second['id']}", headers=auth(admin_token))
    assert _legacy_hash(agent) == hashlib.sha256(first_key.encode()).hexdigest()
    test_client.delete(f"/api/agents/{agent}/keys/{first['id']}", headers=auth(admin_token))
    assert _legacy_hash(agent) not in {hashlib.sha256(k.encode()).hexdigest() for k in (first_key, second_key)}


def _legacy_hash(agent_id):
    with get_db() as conn:
        return conn.execute("SELECT api_key_hash FROM agents WHERE id=?", (agent_id,)).fetchone()[0]


def test_existing_keys_carry_over_unchanged(clean_db):
    """An install from before several keys: the key an agent already holds keeps working, labelled "Original key"."""
    from app.schema import _migrate_029_agent_keys
    from app.services.auth_service import create_user

    create_user("legacy-owner", "lo@test.local", "testpassword123", "LO", "user")
    _, key = agent_service.create_agent("legacy", "Legacy", "legacy-owner")
    with get_db() as conn:
        conn.execute("DROP TABLE agent_keys")
        _migrate_029_agent_keys(conn)
        _migrate_029_agent_keys(conn)  # running it again changes nothing
    keys = agent_service.list_agent_keys("legacy")
    assert [k["label"] for k in keys] == ["Original key"]
    assert agent_service.get_agent_by_api_key(key)["id"] == "legacy"


def test_last_used_is_noted_but_not_on_every_request(test_client, admin_token, agent):
    record, key = add(test_client, admin_token, agent)
    assert alive(test_client, key)
    first = agent_service.get_agent_key(agent, record["id"])["last_used_at"]
    assert first
    assert alive(test_client, key)
    assert agent_service.get_agent_key(agent, record["id"])["last_used_at"] == first  # within the refresh window
    with get_db() as conn:
        conn.execute("UPDATE agent_keys SET last_used_at='2000-01-01T00:00:00+00:00' WHERE id=?", (record["id"],))
    assert alive(test_client, key)
    assert agent_service.get_agent_key(agent, record["id"])["last_used_at"] > "2001"


def test_the_credential_endpoints_refuse_a_revoked_key_and_keep_the_others(test_client, admin_token):
    """The credential endpoints authenticate the key themselves: a revoked key must not pass there either, and a
    key used only there still shows when it was last used."""
    from app.services import credential_service

    owner = "admin"
    agent_service.create_agent("cred-consumer", "Cred", owner, capabilities=["credentials_resolve"], issue_key=False)
    one, one_key = add(test_client, admin_token, "cred-consumer", "one")
    two, two_key = add(test_client, admin_token, "cred-consumer", "two")
    entry = credential_service.create_credential(f"user:{owner}", "Synthetic", "synthetic-fixture-only")
    granted = test_client.post("/api/credential-consumers/grants", headers=auth(admin_token),
                               json={"consumer_agent_id": "cred-consumer", "entry_id": entry["id"]})
    assert granted.status_code == 201, granted.text

    from fastapi.testclient import TestClient

    from app.main import app

    secure = TestClient(app, base_url="https://testserver")  # the credential endpoints are https only

    def resolve(key):
        return secure.post(f"/api/credential-consumers/entries/{entry['id']}/resolve", headers=auth(key))

    assert resolve(one_key).status_code == 200 and resolve(two_key).status_code == 200
    assert agent_service.get_agent_key("cred-consumer", one["id"])["last_used_at"]
    test_client.delete(f"/api/agents/cred-consumer/keys/{one['id']}", headers=auth(admin_token))
    assert resolve(one_key).status_code == 401
    assert resolve(two_key).status_code == 200


def test_rotate_moves_the_old_column_to_the_new_key(test_client, admin_token, agent):
    add(test_client, admin_token, agent)
    new = test_client.post(f"/api/agents/{agent}/rotate_key", headers=auth(admin_token)).json()["data"]["api_key"]
    assert _legacy_hash(agent) == hashlib.sha256(new.encode()).hexdigest()


def test_restoring_a_database_from_before_several_keys_keeps_agents_authenticating(clean_db):
    import sqlite3

    from app.services import backup_service
    from app.services.auth_service import create_user

    create_user("old-owner", "oo@test.local", "testpassword123", "OO", "user")
    _, key = agent_service.create_agent("old-agent", "Old", "old-owner")
    with get_db() as conn:
        conn.execute("DROP TABLE agent_keys")
    from app.database import get_db_path

    backup_service._ensure_agent_keys(str(get_db_path()))
    con = sqlite3.connect(str(get_db_path()))
    try:
        (n,) = con.execute("SELECT COUNT(*) FROM agent_keys WHERE agent_id='old-agent'").fetchone()
    finally:
        con.close()
    assert n == 1 and agent_service.get_agent_by_api_key(key)["id"] == "old-agent"


def test_key_events_are_audited_without_the_key(test_client, admin_token, agent):
    from app.services import audit_service

    record, key = add(test_client, admin_token, agent, "laptop")
    test_client.delete(f"/api/agents/{agent}/keys/{record['id']}", headers=auth(admin_token))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, details_json FROM audit_log WHERE action IN ('agent_key_created','agent_key_revoked') ORDER BY id"
        ).fetchall()
    assert [r["action"] for r in rows] == ["agent_key_created", "agent_key_revoked"]
    for r in rows:
        assert key not in (r["details_json"] or "") and record["id"] in r["details_json"]
    assert "agent_key_created" in audit_service.AUDIT_ACTIONS and "agent_key_revoked" in audit_service.AUDIT_ACTIONS


def test_the_connect_dialog_no_longer_claims_to_replace_keys():
    import pathlib

    js = pathlib.Path("app/dashboard/static/js/integrations.js").read_text()
    assert "invalidates any previous key" not in js and "must be revoked on the Agents page" in js


def test_the_agents_page_lists_keys_and_offers_the_dialog(test_client, admin_token, agent):
    add(test_client, admin_token, agent)
    add(test_client, admin_token, agent, "second")
    page = test_client.get("/agents", headers=auth(admin_token))
    assert page.status_code == 200
    assert "2 keys" in page.text and "data-agent-keys='multi'" in page.text and 'id="keys-modal"' in page.text
