

import json


def test_memory_write(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "The workspace deadline is March 15",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, f"write failed: {r.json()}"
    assert r.json()["ok"] is True


def test_memory_write_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "This should not be accepted",
            "memory_class": "opinion",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_memory_write_rejects_invalid_source_kind(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "This should not be accepted",
            "memory_class": "fact",
            "source_kind": "user_assertion",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_SOURCE_KIND"


def test_memory_write_rejects_invalid_confidence_and_importance(test_client, agent_token):
    confidence = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Invalid confidence",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "confidence": 1.5,
        },
    )
    assert confidence.status_code == 400
    assert confidence.json()["error"]["code"] == "INVALID_CONFIDENCE"

    importance = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Invalid importance",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "importance": -0.1,
        },
    )
    assert importance.status_code == 400
    assert importance.json()["error"]["code"] == "INVALID_IMPORTANCE"


def test_memory_search(test_client, agent_token):
    test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Python is the best language",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "Python", "limit": 10},
    )
    assert r.status_code == 200, f"search failed: {r.json()}"
    assert "records" in r.json()["data"]


def test_memory_import_notes_creates_searchable_external_records(test_client, agent_token):
    import_r = test_client.post(
        "/api/memory/import",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "memory_class": "fact",
            "domain": "import",
            "sources": [
                {
                    "filename": "memory.md",
                    "content": "# Project Notes\n- Workspace: testagent\n- Fact: Remember exact token zephyrdelta-import-token for this workspace.",
                },
                {
                    "filename": "handoff.md",
                    "content": "# Handoff\nUse quasarhandoff-token in handoff searches.",
                },
            ],
        },
    )
    assert import_r.status_code == 201, import_r.json()
    data = import_r.json()["data"]
    assert data["total_records"] == 2
    assert {item["filename"] for item in data["imported"]} == {"memory.md", "handoff.md"}
    assert all(record["source_kind"] == "external_import" for record in data["records"])

    provenance = json.loads(data["records"][0]["provenance_json"])
    assert provenance["route"] == "/api/memory/import"
    assert provenance["source_kind"] == "external_import"
    assert provenance["import_source"] in {"memory.md", "handoff.md"}
    assert provenance["import_chunk"] == 1

    search_r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "zephyrdelta import token", "scope": "agent:testagent"},
    )
    assert search_r.status_code == 200, search_r.json()
    records = search_r.json()["data"]["records"]
    assert any("zephyrdelta-import-token" in record["content"] for record in records)


def test_memory_import_rejects_pii_in_shared_scope_before_writing(test_client, admin_token):
    import_r = test_client.post(
        "/api/memory/import",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scope": "shared",
            "sources": [
                {"filename": "safe.md", "content": "safe import token before rejection"},
                {"filename": "private.md", "content": "contact alice@example.com"},
            ],
        },
    )
    assert import_r.status_code == 422
    assert import_r.json()["error"]["code"] == "PII_DETECTED"

    search_r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"query": "safe import token", "scope": "shared"},
    )
    assert search_r.status_code == 200, search_r.json()
    assert search_r.json()["data"]["records"] == []


def test_memory_page_exposes_import_controls(test_client, admin_token):
    r = test_client.get("/memory", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    html = r.text
    assert "Import Notes" in html
    assert "curated handoffs, decision notes, project facts, or markdown summaries" in html
    assert "What good notes look like" in html
    assert "default save class for imports" in html
    assert 'id="mem-import-files"' in html
    assert 'id="mem-import-scope"' in html
    assert 'id="mem-import-submit"' in html
    assert 'onclick="doImport(event)"' in html


def test_memory_search_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "workspace", "memory_class": "belief"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_memory_get(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Test memory",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    r = test_client.post(
        "/api/memory/get",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "limit": 10},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["records"]) >= 1


def test_memory_search_scope_filter_limits_results(test_client, admin_token):
    user_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Scoped dashboard search token in user scope",
            "memory_class": "fact",
            "scope": "user:admin",
        },
    )
    assert user_r.status_code == 201, user_r.json()

    agent_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Scoped dashboard search token in agent scope",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert agent_r.status_code == 201, agent_r.json()

    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"query": "Scoped dashboard search token", "scope": "agent:testagent"},
    )
    assert r.status_code == 200, r.json()
    records = r.json()["data"]["records"]
    assert records
    assert all(record["scope"] == "agent:testagent" for record in records)


def test_memory_search_uses_domain_topic_confidence_and_importance(test_client, admin_token):
    records_to_write = [
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "confidence": 0.4,
            "importance": 1.0,
        },
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "confidence": 0.9,
            "importance": 0.2,
        },
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "personal",
            "topic": "docker",
            "confidence": 1.0,
            "importance": 1.0,
        },
    ]
    for payload in records_to_write:
        r = test_client.post(
            "/api/memory/write",
            headers={"Authorization": f"Bearer {admin_token}"},
            json=payload,
        )
        assert r.status_code == 201, r.json()

    filtered = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "query": "ranking token",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "min_confidence": 0.8,
        },
    )
    assert filtered.status_code == 200, filtered.json()
    filtered_records = filtered.json()["data"]["records"]
    assert len(filtered_records) == 1
    assert filtered_records[0]["confidence"] == 0.9
    assert filtered_records[0]["domain"] == "engineering"
    assert filtered_records[0]["topic"] == "docker"

    ranked = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "query": "ranking token",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
        },
    )
    assert ranked.status_code == 200, ranked.json()
    ranked_records = ranked.json()["data"]["records"]
    assert len(ranked_records) == 2
    assert ranked_records[0]["importance"] == 1.0
    assert ranked_records[1]["importance"] == 0.2


def test_memory_detail_endpoint_enforces_scope(test_client, admin_token, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Private detail record",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "domain": "validation",
            "topic": "detail",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]

    own_r = test_client.get(
        f"/api/memory/{record_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert own_r.status_code == 200
    assert own_r.json()["data"]["record"]["content"] == "Private detail record"

    other_agent = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "otheragent", "display_name": "Other Agent"},
    )
    assert other_agent.status_code == 201
    other_rotate = test_client.post(
        "/api/agents/otheragent/rotate_key",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert other_rotate.status_code == 200
    other_token = other_rotate.json()["data"]["api_key"]

    denied_r = test_client.get(
        f"/api/memory/{record_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert denied_r.status_code == 403


def test_memory_retract(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "To be retracted",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]
    r = test_client.post(
        f"/api/memory/retract?record_id={record_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200


def test_memory_retract_accepts_json_body_and_uses_correct_error_code(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Retract from body",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]

    r = test_client.post(
        "/api/memory/retract",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"record_id": record_id},
    )
    assert r.status_code == 200

    r2 = test_client.post(
        "/api/memory/retract",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"record_id": record_id},
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "ALREADY_RETRACTED"


def test_memory_delete_removes_embedding_rows(test_client, admin_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Delete with embedding test",
            "memory_class": "fact",
            "scope": "user:admin",
        },
    )
    assert write_r.status_code == 201, write_r.text
    record_id = write_r.json()["data"]["record"]["id"]

    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (record_id, vector, model) VALUES (?, ?, ?)",
            (record_id, b"12345678", "test-model"),
        )
        conn.commit()

    delete_r = test_client.delete(
        f"/api/memory/{record_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete_r.status_code == 200, delete_r.text
    assert delete_r.json()["ok"] is True

    with get_db() as conn:
        record = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
        embedding = conn.execute(
            "SELECT record_id FROM memory_embeddings WHERE record_id = ?",
            (record_id,),
        ).fetchone()
    assert record is None
    assert embedding is None


def test_pii_gate_blocks_shared_scope(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Contact me at alice@example.com",
            "memory_class": "fact",
            "scope": "shared",
        },
    )
    assert r.status_code in (403, 422)


def test_supersession_chain(test_client, agent_token):
    r1 = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Original fact",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r1.status_code == 201
    record1_id = r1.json()["data"]["record"]["id"]
    r2 = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Updated fact",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "supersedes_id": record1_id,
        },
    )
    assert r2.status_code == 201
    assert r2.json()["data"]["record"]["supersedes_id"] == record1_id
    record2_id = r2.json()["data"]["record"]["id"]

    chain_r = test_client.get(
        f"/api/memory/{record2_id}/chain",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert chain_r.status_code == 200
    chain = chain_r.json()["data"]["chain"]
    assert [r["content"] for r in chain] == ["Original fact", "Updated fact"]


def test_memory_search_prefers_active_record_over_superseded_history(
    test_client, admin_token
):
    first = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Current ordering token",
            "memory_class": "fact",
            "scope": "user:admin",
            "importance": 1.0,
        },
    )
    assert first.status_code == 201, first.json()
    first_id = first.json()["data"]["record"]["id"]

    second = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Current ordering token",
            "memory_class": "fact",
            "scope": "user:admin",
            "importance": 0.1,
            "supersedes_id": first_id,
        },
    )
    assert second.status_code == 201, second.json()

    search_r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "query": "Current ordering token",
            "scope": "user:admin",
            "include_superseded": True,
            "limit": 10,
        },
    )
    assert search_r.status_code == 200, search_r.json()
    records = search_r.json()["data"]["records"]
    assert len(records) >= 2
    assert records[0]["record_status"] == "active"
    assert records[0]["id"] == second.json()["data"]["record"]["id"]


def test_memory_write_roundtrips_provenance_and_freshness_fields(
    test_client, agent_token
):
    payload = {
        "content": "Preference with freshness metadata",
        "memory_class": "preference",
        "scope": "agent:testagent",
        "source_kind": "operator_authored",
        "slot_key": "style",
        "valid_from": "2026-05-15T00:00:00Z",
        "valid_to": "2026-06-15T00:00:00Z",
        "last_confirmed_at": "2026-05-15T01:00:00Z",
    }
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=payload,
    )
    assert r.status_code == 201, r.json()
    record = r.json()["data"]["record"]
    assert record["slot_key"] == "style"
    assert record["valid_from"] == "2026-05-15T00:00:00+00:00"
    assert record["valid_to"] == "2026-06-15T00:00:00+00:00"
    assert record["last_confirmed_at"] == "2026-05-15T01:00:00+00:00"
    assert record["provenance_json"]
    provenance = json.loads(record["provenance_json"])
    assert provenance["channel"] == "api"
    assert provenance["source_kind"] == "operator_authored"

    fetched = test_client.get(
        f"/api/memory/{record['id']}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert fetched.status_code == 200, fetched.json()
    fetched_record = fetched.json()["data"]["record"]
    assert fetched_record["slot_key"] == "style"
    assert fetched_record["provenance_json"] == record["provenance_json"]


def test_memory_write_ignores_client_supplied_provenance(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Provenance should be stamped by the server",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "provenance": {
                "actor_id": "spoofed",
                "channel": "client",
                "source_kind": "tool_output",
            },
        },
    )
    assert r.status_code == 201, r.json()
    record = r.json()["data"]["record"]
    provenance = json.loads(record["provenance_json"])
    assert provenance["actor_id"] != "spoofed"
    assert provenance["channel"] == "api"
    assert provenance["route"] == "/api/memory/write"


def test_preference_slot_key_supersedes_previous_active_preference(
    test_client, agent_token
):
    first = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Use concise responses",
            "memory_class": "preference",
            "scope": "agent:testagent",
            "slot_key": "style",
        },
    )
    assert first.status_code == 201, first.json()
    first_id = first.json()["data"]["record"]["id"]

    second = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Use detailed responses",
            "memory_class": "preference",
            "scope": "agent:testagent",
            "slot_key": "style",
        },
    )
    assert second.status_code == 201, second.json()
    second_record = second.json()["data"]["record"]
    assert second_record["supersedes_id"] == first_id

    first_fetched = test_client.get(
        f"/api/memory/{first_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert first_fetched.status_code == 200, first_fetched.json()
    assert first_fetched.json()["data"]["record"]["record_status"] == "superseded"

    chain_r = test_client.get(
        f"/api/memory/{second_record['id']}/chain",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert chain_r.status_code == 200, chain_r.json()
    chain = chain_r.json()["data"]["chain"]
    assert [r["content"] for r in chain] == [
        "Use concise responses",
        "Use detailed responses",
    ]
