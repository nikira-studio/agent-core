import pytest
import json
from unittest.mock import patch, MagicMock


def test_search_returns_fts_only_when_embedding_unavailable(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Important project details for Q1 launch",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201

    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status:
        mock_status.return_value = {"backend": "unavailable", "model_configured": False}
        search_r = test_client.post(
            "/api/memory/search",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"query": "Q1 launch", "limit": 10},
        )

    assert search_r.status_code == 200
    data = search_r.json()["data"]
    assert data["retrieval_mode"] == "fts_only"
    assert "embedding_backend_status" in data


def test_search_returns_fts_only_empty_query(test_client, agent_token):
    search_r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "", "limit": 10},
    )
    assert search_r.status_code == 400


def test_embedding_write_with_mocked_healthy_backend(test_client, agent_token):
    mock_vector_bytes = b"\x00" * (384 * 4)
    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status, \
         patch("app.services.embedding_service.generate_embedding") as mock_gen, \
         patch("app.services.vector_service.is_sqlite_vec_available") as mock_vec:
        mock_status.return_value = {"backend": "healthy", "model_configured": True}
        mock_gen.return_value = (mock_vector_bytes, "ok")
        mock_vec.return_value = True

        with patch("app.services.vector_service.store_embedding") as mock_store:
            write_r = test_client.post(
                "/api/memory/write",
                headers={"Authorization": f"Bearer {agent_token}"},
                json={
                    "content": "A test memory that should generate an embedding",
                    "memory_class": "fact",
                    "scope": "agent:testagent",
                },
            )
            assert write_r.status_code == 201
            record_id = write_r.json()["data"]["record"]["id"]
            mock_store.assert_called()


def test_search_returns_hybrid_mode_with_semantic_results(test_client, agent_token):
    mock_vector_bytes = b"\x00" * (384 * 4)

    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status, \
         patch("app.services.embedding_service.generate_embedding") as mock_gen, \
         patch("app.services.vector_service.is_sqlite_vec_available") as mock_vec, \
         patch("app.services.vector_service.cosine_search_top_k") as mock_cosine:

        mock_status.return_value = {"backend": "healthy", "model_configured": True}
        mock_gen.return_value = (mock_vector_bytes, "ok")
        mock_vec.return_value = True
        mock_cosine.return_value = []

        write_r = test_client.post(
            "/api/memory/write",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "content": "Important planning decision",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        )
        assert write_r.status_code == 201
        record_id = write_r.json()["data"]["record"]["id"]

    mock_cosine2 = MagicMock(return_value=[(record_id, 0.93)])
    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status, \
         patch("app.services.embedding_service.generate_embedding") as mock_gen, \
         patch("app.services.vector_service.is_sqlite_vec_available") as mock_vec, \
         patch("app.services.vector_service.cosine_search_top_k", mock_cosine2):
        mock_status.return_value = {"backend": "healthy", "model_configured": True}
        mock_gen.return_value = (mock_vector_bytes, "ok")
        mock_vec.return_value = True

        search_r = test_client.post(
            "/api/memory/search",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"query": "planning decision", "limit": 10},
        )

    assert search_r.status_code == 200
    data = search_r.json()["data"]
    assert data["retrieval_mode"] == "hybrid"
    assert [r["id"] for r in data["records"]] == [record_id]


def test_search_includes_embedding_backend_status_in_response(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "test query for status", "limit": 10},
    )
    assert r.status_code == 200
    assert "embedding_backend_status" in r.json()["data"]


def test_search_degraded_mode_when_embedding_fails(test_client, agent_token):
    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status:
        mock_status.return_value = {"backend": "unavailable", "model_configured": False}

        r = test_client.post(
            "/api/memory/write",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "content": "Memory that will not get semantic search",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        )
        assert r.status_code == 201

    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status:
        mock_status.return_value = {"backend": "unavailable", "model_configured": False}

        search_r = test_client.post(
            "/api/memory/search",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"query": "semantic search test", "limit": 10},
        )

    assert search_r.status_code == 200
    data = search_r.json()["data"]
    assert data["retrieval_mode"] == "fts_only"
    assert data["embedding_backend_status"] == "unavailable"


def test_mcp_memory_search_returns_retrieval_mode_and_backend_status(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "any query", "limit": 5},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "retrieval_mode" in data
    assert "embedding_backend_status" in data


def test_mcp_tool_memory_search_returns_embedding_status(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_search", "params": {"query": "test", "limit": 5}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "retrieval_mode" in data
    assert "embedding_backend_status" in data


def test_mcp_tool_memory_search_empty_scopes_returns_embedding_status(test_client, admin_token):
    create_r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": "noscope-agent",
            "display_name": "No Scope Agent",
            "read_scopes": [],
            "write_scopes": [],
        },
    )
    assert create_r.status_code == 201
    rotate_r = test_client.post(
        "/api/agents/noscope-agent/rotate_key",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert rotate_r.status_code == 200
    no_scope_key = rotate_r.json()["data"]["api_key"]

    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status:
        mock_status.return_value = {"backend": "unavailable", "model_configured": False}
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {no_scope_key}"},
            json={"tool": "memory_search", "params": {"query": "anything", "limit": 5}},
        )

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["records"] == []
    assert data["retrieval_mode"] == "fts_only"
    assert data["embedding_backend_status"] == "unavailable"


def test_memory_search_audit_logs_retrieval_mode(test_client, agent_token, clean_db):
    with patch("app.services.embedding_service.get_embedding_backend_status") as mock_status:
        mock_status.return_value = {"backend": "unavailable", "model_configured": False}

        r = test_client.post(
            "/api/memory/search",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"query": "audit test query", "limit": 5},
        )
        assert r.status_code == 200

    from app.services import audit_service

    events = audit_service.query_events(action="memory_search")
    assert len(events) >= 1
    details = json.loads(events[0]["details_json"])
    assert details["retrieval_mode"] == "fts_only"
    assert details["embedding_backend_status"] == "unavailable"

    degraded_events = audit_service.query_events(action="retrieval_degraded")
    assert len(degraded_events) >= 1
