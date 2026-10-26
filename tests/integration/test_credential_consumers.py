"""Synthetic production-shaped consumer keys and owner sessions only."""
import json
from fastapi.testclient import TestClient
import pytest

from app.database import get_db
from app.security.encryption import encrypt_value
from app.main import app
from app.services import agent_service, credential_service, credential_consumer_service


@pytest.fixture
def consumer(test_client, admin_token):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users WHERE email=?", ("admin@test.local",)).fetchone()[0]
    agent, key = agent_service.create_agent("consumer", "Consumer", owner, capabilities=["credentials_resolve"])
    entry = credential_service.create_credential(f"user:{owner}", "Synthetic", "synthetic-fixture-only")
    with TestClient(app, base_url="https://testserver") as client:
        yield client, admin_token, owner, agent, key, entry


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def grant(c):
    client, token, owner, agent, key, entry = c
    response = client.post("/api/credential-consumers/grants", headers=auth(token), json={"consumer_agent_id": agent["id"], "entry_id": entry["id"]})
    assert response.status_code == 201, response.text
    return response.json()["data"]["grant"]


def resolve(c, entry=None):
    client, _, _, _, key, saved = c
    return client.post(f"/api/credential-consumers/entries/{entry or saved['id']}/resolve", headers=auth(key))


def test_default_deny_even_with_inherited_user_scope(consumer):
    assert resolve(consumer).status_code == 403
    grant(consumer)
    response = resolve(consumer)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["value"] == "synthetic-fixture-only"
    assert response.json()["data"]["revalidate_after_seconds"] == 30


def test_other_entry_and_other_consumer_denied(consumer):
    grant(consumer)
    client, token, owner, _, _, entry = consumer
    other = credential_service.create_credential(f"user:{owner}", "Other", "other-synthetic")
    assert resolve(consumer, other["id"]).status_code == 403
    _, key = agent_service.create_agent("other-consumer", "Other", owner, capabilities=["credentials_resolve"])
    assert client.post(f"/api/credential-consumers/entries/{entry['id']}/resolve", headers=auth(key)).status_code == 403


def test_no_list_crud_reveal_memory_connector_or_admin(consumer):
    grant(consumer)
    client, _, _, _, key, entry = consumer
    for method, path, body in [
        ("get", "/api/credentials/entries", None),
        ("post", f"/api/credentials/entries/{entry['id']}/reveal", None),
        ("post", "/api/credentials/entries", {"scope": "agent:consumer", "name": "x", "value": "fixture"}),
        ("post", "/api/memory/search", {"query":"synthetic"}),
        ("get", "/api/connector-bindings", None),
        ("post", "/api/credential-consumers/grants", {"consumer_agent_id": "consumer", "entry_id": entry["id"]}),
    ]:
        r = client.request(method, path, headers=auth(key), json=body)
        assert r.status_code in (401,403), (path,r.text)
    from app.security.scope_enforcer import build_agent_context
    ctx = build_agent_context(agent_service.get_agent_by_id("consumer"))
    assert ctx.capabilities == frozenset({"credentials_resolve"})


def test_revocation_and_key_rotation_are_immediate(consumer):
    g = grant(consumer)
    client, token, owner, agent, old, entry = consumer
    assert resolve(consumer).status_code == 200
    r = client.delete(f"/api/credential-consumers/grants/{g['id']}", headers=auth(token))
    assert r.status_code == 200
    assert resolve(consumer).status_code == 403
    grant(consumer)
    agent_service.rotate_agent_key(agent["id"])
    assert resolve(consumer).status_code == 401


@pytest.mark.parametrize("expiry", ["2000-01-01T00:00:00+00:00", "malformed"])
def test_expired_or_malformed_grant_denied(consumer, expiry):
    g = grant(consumer)
    with get_db() as conn:
        conn.execute("UPDATE credential_consumer_grants SET expires_at=? WHERE id=?", (expiry,g["id"]))
    assert resolve(consumer).status_code == 403


@pytest.mark.parametrize("value,expiry", [("",None),("  ",None),("fixture","malformed"),("fixture","2000-01-01T00:00:00+00:00")])
def test_unready_entry_denied(consumer,value,expiry):
    grant(consumer)
    entry=consumer[-1]
    credential_service.update_credential(entry["id"],value_encrypted=encrypt_value(value),expires_at=expiry)
    assert resolve(consumer).status_code == 409


def test_disabled_agent_denied(consumer):
    grant(consumer)
    with get_db() as conn:
        conn.execute("UPDATE agents SET is_active=0 WHERE id='consumer'")
    assert resolve(consumer).status_code == 401


def test_status_excludes_value_and_version_changes(consumer):
    grant(consumer)
    client,_,_,_,key,entry=consumer
    path=f"/api/credential-consumers/entries/{entry['id']}/status"
    before=client.post(path,headers=auth(key)).json()["data"]
    assert "value" not in before
    credential_service.update_credential(entry["id"],value_encrypted=encrypt_value("replacement-synthetic"))
    after=client.post(path,headers=auth(key)).json()["data"]
    assert before["version"] != after["version"]


def test_batched_lease_status_checks_exact_grants_without_decrypt_or_success_audit(consumer,monkeypatch):
    client,token,owner,agent,key,entry=consumer
    grant(consumer)
    second=credential_service.create_credential(f"user:{owner}","Synthetic second","second-synthetic-only")
    r=client.post("/api/credential-consumers/grants",headers=auth(token),json={"consumer_agent_id":agent["id"],"entry_id":second["id"]})
    assert r.status_code==201
    before=credential_consumer_service.decrypt_value
    def forbidden(_): raise AssertionError("lease status must not decrypt")
    monkeypatch.setattr(credential_consumer_service,"decrypt_value",forbidden)
    with get_db() as conn:
        prior=conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_type='agent' AND actor_id=? AND action='credential_consumer_resolved'",(agent["id"],)).fetchone()[0]
    response=client.post("/api/credential-consumers/leases/status",headers=auth(key),json={"entry_ids":[entry["id"],second["id"]]})
    assert response.status_code==200
    data=response.json()["data"]["entries"]
    assert [x["entry_id"] for x in data]==[entry["id"],second["id"]]
    assert all("value" not in x and x["revalidate_after_seconds"]==30 for x in data)
    with get_db() as conn:
        after=conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_type='agent' AND actor_id=? AND action='credential_consumer_resolved'",(agent["id"],)).fetchone()[0]
    assert after==prior
    monkeypatch.setattr(credential_consumer_service,"decrypt_value",before)


def test_batched_lease_status_is_atomic_and_bounded(consumer):
    client,token,owner,agent,key,entry=consumer
    grant(consumer)
    other=credential_service.create_credential(f"user:{owner}","Ungrant","ungranted-synthetic")
    path="/api/credential-consumers/leases/status"
    assert client.post(path,headers=auth(key),json={"entry_ids":[entry["id"],other["id"]]}).status_code==403
    assert client.post(path,headers=auth(key),json={"entry_ids":[entry["id"],entry["id"]]}).status_code==400
    assert client.post(path,headers=auth(key),json={"entry_ids":[]}).status_code==422
    assert client.post(path,headers=auth(key),json={"entry_ids":[entry["id"]]*65}).status_code==422


def test_no_secret_in_audit_or_logs(consumer,caplog):
    grant(consumer)
    assert resolve(consumer).status_code==200
    assert resolve(consumer,"unknown").status_code==403
    with get_db() as conn:
        events=[dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action LIKE 'credential_consumer_%'")]
    serialized=json.dumps(events)
    assert "synthetic-fixture-only" not in serialized+caplog.text
    assert consumer[-2] not in serialized+caplog.text
    assert {r["action"] for r in events} >= {"credential_consumer_grant_created","credential_consumer_resolved","credential_consumer_denied"}


def test_https_and_no_human_token_fallback(consumer):
    grant(consumer)
    client,token,_,_,key,entry=consumer
    path=f"/api/credential-consumers/entries/{entry['id']}/resolve"
    assert client.post(path,headers=auth(token)).status_code==401
    with TestClient(app,base_url="http://testserver") as plain:
        assert plain.post(path,headers=auth(key)).status_code==403


def test_new_capability_not_added_to_legacy_defaults():
    assert "credentials_resolve" not in agent_service.normalize_capabilities(None)
    assert agent_service.normalize_capabilities(["credentials_resolve"]) == ["credentials_resolve"]


def test_malformed_capability_shape_denied(consumer):
    grant(consumer)
    with get_db() as conn:
        conn.execute("UPDATE agents SET capabilities_json=? WHERE id='consumer'", ('"credentials_resolve"',))
    assert resolve(consumer).status_code == 403


def test_invalid_grant_and_forbidden_selected_identity(consumer):
    client,token,_,_,key,entry=consumer
    body={"consumer_agent_id":"consumer","entry_id":entry["id"],"expires_at":"malformed"}
    assert client.post("/api/credential-consumers/grants",headers=auth(token),json=body).status_code==400
    body["expires_at"]="2000-01-01T00:00:00Z"
    assert client.post("/api/credential-consumers/grants",headers=auth(token),json=body).status_code==400
    body.pop("expires_at")
    body["actor_id"]="another-agent"
    assert client.post("/api/credential-consumers/grants",headers=auth(token),json=body).status_code==422


def test_resolve_serializes_with_concurrent_revocation(consumer,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.security.exceptions import APIError
    g=grant(consumer)
    _,_,owner,_,key,entry=consumer
    entered=threading.Event()
    release=threading.Event()
    original=credential_consumer_service.decrypt_value
    def held(ciphertext):
        entered.set()
        assert release.wait(3)
        return original(ciphertext)
    monkeypatch.setattr(credential_consumer_service,"decrypt_value",held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(credential_consumer_service.resolve,key,entry["id"])
        assert entered.wait(3)
        revoking=pool.submit(credential_consumer_service.revoke_grant,g["id"],owner)
        assert not revoking.done()
        release.set()
        assert reading.result()["value"]=="synthetic-fixture-only"
        assert revoking.result()["revoked"]
    with pytest.raises(APIError):
        credential_consumer_service.resolve(key,entry["id"])


def test_duplicate_active_grant_rejected_and_schema_reinit_keeps_revocation(consumer):
    from app.database import init_db
    g=grant(consumer)
    client,token,owner,agent,key,entry=consumer
    r=client.post("/api/credential-consumers/grants",headers=auth(token),json={"consumer_agent_id":agent["id"],"entry_id":entry["id"]})
    assert r.status_code==409
    credential_consumer_service.revoke_grant(g["id"],owner)
    init_db()
    assert resolve(consumer).status_code==403
    with get_db() as conn:
        assert conn.execute("SELECT revoked_at FROM credential_consumer_grants WHERE id=?",(g["id"],)).fetchone()[0]


def test_backup_copy_cannot_resurrect_machine_grants(consumer,tmp_path):
    import sqlite3
    from app import database
    from app.services.backup_service import _revoke_consumer_grants
    g=grant(consumer)
    copy=tmp_path/"isolated-backup.db"
    with get_db() as source:
        with sqlite3.connect(copy) as target:
            source.backup(target)
    _revoke_consumer_grants(str(copy))
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT revoked_at FROM credential_consumer_grants WHERE id=?",(g["id"],)).fetchone()[0]
    assert resolve(consumer).status_code==200
    original=database.DB_PATH_OVERRIDE
    try:
        database.DB_PATH_OVERRIDE=str(copy)
        assert resolve(consumer).status_code==403
    finally:
        database.DB_PATH_OVERRIDE=original

def write_grant(c):
    client, token, _, agent, _, entry = c
    r = client.post(
        "/api/credential-consumers/grants",
        headers=auth(token),
        json={
            "consumer_agent_id": agent["id"],
            "entry_id": entry["id"],
            "allow_write": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["grant"]["allow_write"] == 1
    return r.json()["data"]["grant"]


def write_value(
    c,
    request_id="synthetic-write",
    version=None,
    value="synthetic-refreshed",
    entry_id=None,
):
    client, _, _, _, key, entry = c
    if version is None:
        version = resolve(c).json()["data"]["version"]
    return client.post(
        f"/api/credential-consumers/entries/{entry_id or entry['id']}/write",
        headers=auth(key),
        json={"request_id": request_id, "expected_version": version, "value": value},
    )


def test_read_grant_cannot_write_and_legacy_default_is_read_only(consumer):
    grant(consumer)
    r = write_value(consumer)
    assert r.status_code == 403
    assert resolve(consumer).json()["data"]["value"] == "synthetic-fixture-only"
    with get_db() as db:
        assert (
            db.execute("select allow_write from credential_consumer_grants").fetchone()[
                0
            ]
            == 0
        )
        assert (
            db.execute("select count(*) from credential_consumer_writes").fetchone()[0]
            == 0
        )


def test_write_replay_is_atomic_without_reencrypting_and_payload_reuse_denied(consumer):
    write_grant(consumer)
    version = resolve(consumer).json()["data"]["version"]
    first = write_value(consumer, version=version)
    assert first.status_code == 200
    again = write_value(consumer, version=version)
    assert again.json() == first.json()
    assert first.headers["cache-control"] == "no-store"
    assert (
        write_value(consumer, version=version, value="different-synthetic").status_code
        == 409
    )
    now = resolve(consumer).json()["data"]
    assert now["value"] == "synthetic-refreshed"
    assert now["version"] == first.json()["data"]["version"]
    with get_db() as db:
        assert (
            db.execute("select count(*) from credential_consumer_writes").fetchone()[0]
            == 1
        )


def test_conflict_preserves_encrypted_candidate_blocks_every_consumer_and_replays(
    consumer, caplog
):
    from app.security.encryption import decrypt_value

    write_grant(consumer)
    old = resolve(consumer).json()["data"]["version"]
    assert (
        write_value(
            consumer,
            request_id="winner",
            version=old,
            value="synthetic-winning-refresh",
        ).status_code
        == 200
    )
    conflict = write_value(
        consumer, request_id="loser", version=old, value="synthetic-competing-refresh"
    )
    assert conflict.status_code == 409
    data = conflict.json()["data"]
    assert data["state"] == "conflict_retained"
    assert (
        write_value(
            consumer,
            request_id="loser",
            version=old,
            value="synthetic-competing-refresh",
        ).json()
        == conflict.json()
    )
    assert resolve(consumer).status_code == 409
    client, _, owner, _, _, entry = consumer
    _, other_key = agent_service.create_agent(
        "second-reader", "Second", owner, capabilities=["credentials_resolve"]
    )
    credential_consumer_service.create_grant("second-reader", entry["id"], owner)
    for suffix in ["resolve", "status"]:
        assert (
            client.post(
                f"/api/credential-consumers/entries/{entry['id']}/{suffix}",
                headers=auth(other_key),
            ).status_code
            == 409
        )
    assert resolve(consumer, data["candidate_entry_id"]).status_code == 403
    with get_db() as db:
        candidate = db.execute(
            "select value_encrypted from credentials where id=?",
            (data["candidate_entry_id"],),
        ).fetchone()[0]
        assert "synthetic-competing-refresh" not in candidate
        assert decrypt_value(candidate) == "synthetic-competing-refresh"
        events = json.dumps([dict(r) for r in db.execute("select * from audit_log")])
        requests = json.dumps(
            [dict(r) for r in db.execute("select * from credential_consumer_writes")]
        )
    for secret in [
        "synthetic-winning-refresh",
        "synthetic-competing-refresh",
        consumer[-2],
    ]:
        assert secret not in events + requests + conflict.text + caplog.text


def test_cached_write_denied_after_revocation_or_key_rotation(consumer):
    g = write_grant(consumer)
    v = resolve(consumer).json()["data"]["version"]
    assert write_value(consumer, version=v).status_code == 200
    credential_consumer_service.revoke_grant(g["id"], consumer[2])
    assert write_value(consumer, version=v).status_code == 403
    write_grant(consumer)
    agent_service.rotate_agent_key(consumer[3]["id"])
    assert write_value(consumer, version=v).status_code == 401


def test_write_unknown_entry_inherited_scope_and_malformed_payload_fail_closed(
    consumer,
):
    write_grant(consumer)
    v = resolve(consumer).json()["data"]["version"]
    assert (
        write_value(consumer, version=v, entry_id="ungranted-entry").status_code == 403
    )
    client, _, _, _, key, entry = consumer
    path = f"/api/credential-consumers/entries/{entry['id']}/write"
    for data in [
        {
            "request_id": "bad",
            "expected_version": v,
            "value": {"nested": "synthetic-must-not-echo"},
        },
        {"unexpected": "synthetic-must-not-echo"},
    ]:
        r = client.post(path, headers=auth(key), json=data)
        assert r.status_code == 400
        assert "synthetic-must-not-echo" not in r.text


def test_concurrent_writes_keep_both_results_and_no_blind_overwrite(consumer):
    from concurrent.futures import ThreadPoolExecutor
    from app.security.encryption import decrypt_value

    write_grant(consumer)
    v = resolve(consumer).json()["data"]["version"]
    key = consumer[-2]
    entry = consumer[-1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                credential_consumer_service.write,
                key,
                entry["id"],
                f"concurrent-{i}",
                v,
                f"synthetic-response-{i}",
            )
            for i in range(2)
        ]
        results = [f.result() for f in futures]
    assert sorted(r["status_code"] for r in results) == [200, 409]
    with get_db() as db:
        values = {
            decrypt_value(r[0])
            for r in db.execute("select value_encrypted from credentials")
        }
    assert values == {"synthetic-response-0", "synthetic-response-1"}
    assert resolve(consumer).status_code == 409


def test_candidate_participates_in_rekey_and_sqlite_backup(consumer, tmp_path):
    import sqlite3
    from app.security.encryption import decrypt_value
    from app.services.credential_rotation_service import rotate_key

    write_grant(consumer)
    old = resolve(consumer).json()["data"]["version"]
    assert write_value(consumer, request_id="first", version=old).status_code == 200
    conflict = write_value(
        consumer, request_id="other", version=old, value="synthetic-retained-candidate"
    ).json()["data"]
    ok, _, detail = rotate_key(consumer[2])
    assert ok
    assert detail["re_encrypted_count"] == 2
    copy = tmp_path / "ciphertext-only.sqlite"
    with get_db() as source:
        with sqlite3.connect(copy) as target:
            source.backup(target)
    with sqlite3.connect(copy) as db:
        encrypted = db.execute(
            "select value_encrypted from credentials where id=?",
            (conflict["candidate_entry_id"],),
        ).fetchone()[0]
    assert decrypt_value(encrypted) == "synthetic-retained-candidate"
    assert b"synthetic-retained-candidate" not in copy.read_bytes()


def test_write_uses_key_operation_lock_and_migration_preserves_read_only(
    consumer, monkeypatch
):
    from app.security.encryption import KEY_OPERATION_LOCK

    original = credential_consumer_service.encrypt_value

    def checked(value):
        assert KEY_OPERATION_LOCK._is_owned()
        return original(value)

    monkeypatch.setattr(credential_consumer_service, "encrypt_value", checked)
    write_grant(consumer)
    assert write_value(consumer).status_code == 200
    import sqlite3
    from app.schema import _migrate_006_consumer_write_grants

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("create table credential_consumer_grants(id text)")
    db.execute("insert into credential_consumer_grants values('legacy')")
    _migrate_006_consumer_write_grants(db)
    assert (
        db.execute("select allow_write from credential_consumer_grants").fetchone()[0]
        == 0
    )


def test_conflict_candidate_count_and_payload_size_bounded(consumer):
    write_grant(consumer)
    entry = consumer[-1]
    for i in range(3):
        credential_service.create_credential(
            entry["scope"],
            f"Synthetic candidate {i}",
            f"synthetic-{i}",
            metadata_json=json.dumps({"consumer_conflict_parent": entry["id"]}),
        )
    r = write_value(consumer, version="0" * 64)
    assert r.status_code == 409
    assert "CONFLICT_STORAGE_FULL" in r.text
    assert resolve(consumer).status_code == 409
    client, _, _, _, key, saved = consumer
    assert client.post(f"/api/credential-consumers/entries/{saved['id']}/status", headers=auth(key)).status_code == 409
    assert write_value(consumer, version="0" * 64, value="x" * 65537).status_code == 400
    with get_db() as db:
        assert db.execute("select count(*) from credentials").fetchone()[0] == 4
