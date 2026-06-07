"""Phase 4: atomic cross-scope memory_move (service + MCP + REST)."""

import json

from app.services import memory_service


def _write(scope, content="movable content", topic="move-topic", cls="fact"):
    record, err = memory_service.write_memory(
        content=content,
        memory_class=cls,
        scope=scope,
        domain="d",
        topic=topic,
        confidence=0.8,
        importance=0.6,
    )
    assert err is None
    return record


class TestMoveMemoryService:
    def test_move_relocates_and_preserves_lineage(self, clean_db):
        old = _write("workspace:src")
        new, err = memory_service.move_memory(
            old["id"],
            "workspace:dst",
            provenance_json=json.dumps({"actor_id": "tester"}),
        )
        assert err is None

        # New record: active in destination, same content/class/topic, lineage set.
        assert new["scope"] == "workspace:dst"
        assert new["record_status"] == "active"
        assert new["content"] == old["content"]
        assert new["memory_class"] == old["memory_class"]
        assert new["topic"] == old["topic"]
        assert new["confidence"] == old["confidence"]
        assert new["supersedes_id"] == old["id"]
        new_prov = json.loads(new["provenance_json"])
        assert new_prov["actor_id"] == "tester"
        assert new_prov["moved_from"]["record_id"] == old["id"]
        assert new_prov["moved_from"]["scope"] == "workspace:src"

        # Original: retracted, points to the new record.
        refreshed = memory_service.get_memory_record(old["id"])
        assert refreshed["record_status"] == "retracted"
        assert refreshed["superseded_by_id"] == new["id"]
        old_prov = json.loads(refreshed["provenance_json"])
        assert old_prov["moved_to"]["record_id"] == new["id"]
        assert old_prov["moved_to"]["scope"] == "workspace:dst"

        # Destination active set contains the new record; source no longer does.
        dst_active = memory_service.get_memory_by_scope("workspace:dst")
        assert any(r["id"] == new["id"] for r in dst_active)
        src_active = memory_service.get_memory_by_scope(
            "workspace:src", record_status="active"
        )
        assert all(r["id"] != old["id"] for r in src_active)

    def test_move_missing_record(self, clean_db):
        result, err = memory_service.move_memory("nope", "workspace:dst")
        assert result is None
        assert err == "NOT_FOUND"

    def test_move_same_scope_rejected(self, clean_db):
        old = _write("workspace:src")
        result, err = memory_service.move_memory(old["id"], "workspace:src")
        assert result is None
        assert err == "SAME_SCOPE"

    def test_move_non_active_rejected(self, clean_db):
        old = _write("workspace:src")
        memory_service.retract_memory(old["id"])
        result, err = memory_service.move_memory(old["id"], "workspace:dst")
        assert result is None
        assert err == "NOT_ACTIVE"


class TestMoveMemoryMCP:
    def test_mcp_move_as_admin(self, test_client, admin_token):
        old = _write("workspace:src", content="mcp move me", topic="mcp-move")
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "memory_move",
                "params": {"record_id": old["id"], "new_scope": "workspace:dst"},
            },
        )
        assert r.status_code == 201, r.text
        new = r.json()["data"]["record"]
        assert new["scope"] == "workspace:dst"
        assert new["content"] == "mcp move me"
        assert new["supersedes_id"] == old["id"]
        assert memory_service.get_memory_record(old["id"])["record_status"] == "retracted"

    def test_mcp_move_denied_without_write_on_source(self, test_client, agent_token):
        # agent_token can write agent:testagent but not these workspaces.
        old = _write("workspace:src")
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "memory_move",
                "params": {"record_id": old["id"], "new_scope": "workspace:dst"},
            },
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "SCOPE_DENIED"
        # Source untouched.
        assert memory_service.get_memory_record(old["id"])["record_status"] == "active"


class TestMoveMemoryRest:
    def test_rest_move(self, test_client, admin_token):
        old = _write("workspace:src", content="rest move me", topic="rest-move")
        r = test_client.post(
            "/api/memory/move",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"record_id": old["id"], "new_scope": "workspace:dst"},
        )
        assert r.status_code == 201, r.text
        new = r.json()["data"]["record"]
        assert new["scope"] == "workspace:dst"
        assert new["supersedes_id"] == old["id"]
        assert memory_service.get_memory_record(old["id"])["record_status"] == "retracted"

    def test_rest_move_requires_new_scope(self, test_client, admin_token):
        old = _write("workspace:src")
        r = test_client.post(
            "/api/memory/move",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"record_id": old["id"]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "MISSING_NEW_SCOPE"
