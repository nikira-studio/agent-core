"""Exercise response sizing at both public search boundaries."""

import json

import pytest

from app.database import get_db
from app.security.rate_limiter import CSG
from app.services import memory_service, embedding_service


def _size(data):
    return len(json.dumps(data, indent=2, default=str))


@pytest.fixture
def search(test_client, agent_token, monkeypatch):
    monkeypatch.setattr(
        embedding_service,
        "safe_backend_status",
        lambda: {"backend": "disabled", "model_configured": False},
    )
    ids = []
    for index in range(3):
        record, error = memory_service.write_memory(
            content=f"budgetmarker {index}: " + '日本語 "quoted" \\ path\n' * 90,
            memory_class="decision",
            scope="agent:testagent",
            provenance_json=json.dumps({"source": "metadata " * 160}),
        )
        assert error is None
        ids.append(record["id"])

    def request(endpoint="/mcp", **params):
        headers = {"Authorization": f"Bearer {agent_token}"}
        args = {"query": "budgetmarker", **params}
        return test_client.post(
            endpoint,
            headers=headers,
            json={"tool": "memory_search", "params": args}
            if endpoint == "/mcp"
            else args,
        )

    return request, ids


def _reset_recalls():
    with get_db() as conn:
        conn.execute("UPDATE memory_records SET recall_count=0,last_recalled_at=NULL")
        conn.commit()


@pytest.mark.parametrize("endpoint", ["/mcp", "/api/memory/search"])
@pytest.mark.parametrize("view", ["lean", "full"])
def test_budget_exact_boundary_returns_prefix_and_counts_only_that_prefix(
    search, endpoint, view
):
    request, ids = search
    full = request(endpoint, view=view, max_response_chars=100000).json()["data"]
    _reset_recalls()
    expected = {
        **full,
        "records": full["records"][:1],
        "total": 1,
        "response_limited": True,
    }
    budget = _size(expected)
    response = request(endpoint, view=view, max_response_chars=budget)
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data == expected
    assert _size(data) == budget
    with get_db() as conn:
        counts = {
            r["id"]: r["recall_count"]
            for r in conn.execute("SELECT id,recall_count FROM memory_records")
        }
        audit = json.loads(
            conn.execute(
                "SELECT details_json FROM audit_log WHERE action='memory_search' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert counts[expected["records"][0]["id"]] == 1
    assert sum(counts.values()) == 1
    assert audit["results"] == 1
    assert set(counts) == set(ids)
    if endpoint.startswith("/api/"):
        assert "x-ratelimit-limit" in response.headers
        assert CSG.get_active("testagent") == 0


@pytest.mark.parametrize("endpoint", ["/mcp", "/api/memory/search"])
def test_oversized_first_record_is_explicit_and_not_clipped(search, endpoint):
    request, _ = search
    _reset_recalls()
    response = request(endpoint, max_response_chars=600)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["records"] == [] and data["response_limited"] is True
    assert data["warnings"][0]["code"] == "RESPONSE_BUDGET_TOO_SMALL"
    assert _size(data) <= 600
    with get_db() as conn:
        assert (
            conn.execute("SELECT sum(recall_count) FROM memory_records").fetchone()[0]
            == 0
        )
    response = request(endpoint, max_response_chars=10)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RESPONSE_BUDGET_TOO_SMALL"
    assert CSG.get_active("testagent") == 0


@pytest.mark.parametrize("endpoint", ["/mcp", "/api/memory/search"])
def test_strict_budget_validation_and_null_compatibility(search, endpoint):
    request, _ = search
    for bad in (True, False, 0, -1, 1.5, "1000"):
        response = request(endpoint, max_response_chars=bad)
        assert response.status_code in (400, 422), (bad, response.json())
    omitted = request(endpoint).json()["data"]
    null = request(endpoint, max_response_chars=None).json()["data"]
    assert null == omitted
    enough = request(endpoint, max_response_chars=100000).json()["data"]
    if endpoint == "/mcp":
        # MCP defaults the budget to the offload threshold (see the next test).
        assert omitted["response_limited"] is True
    else:
        assert "response_limited" not in null
        assert enough == {**omitted, "response_limited": False}


@pytest.mark.parametrize("endpoint", ["/mcp", "/api/memory/search"])
def test_empty_and_no_scope_responses_use_budget(search, monkeypatch, endpoint):
    request, _ = search
    empty = request(endpoint, query="nonexistent", max_response_chars=300)
    assert empty.status_code == 200
    assert empty.json()["data"]["response_limited"] is False
    from app.security.effective_authority import EffectiveAuthority

    monkeypatch.setattr(EffectiveAuthority, "filter_scopes", lambda *args: [])
    response = request(endpoint, max_response_chars=300)
    assert response.status_code == 200
    assert response.json()["data"]["records"] == []
    assert response.json()["data"]["response_limited"] is False
    assert request(endpoint, max_response_chars=1).status_code == 400


def test_budget_on_real_jsonrpc_text_and_spill_recall(
    search, test_client, agent_token, monkeypatch
):
    request, ids = search
    from app.config import settings

    full = request(max_response_chars=100000).json()["data"]
    first = {
        **full,
        "records": full["records"][:1],
        "total": 1,
        "response_limited": True,
    }

    def call(name, args):
        response = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        assert response.status_code == 200, response.json()
        return response.json()["result"]["content"][0]["text"]

    _reset_recalls()
    text = call(
        "memory_search", {"query": "budgetmarker", "max_response_chars": _size(first)}
    )
    assert len(text) == _size(first)
    assert json.loads(text) == first
    _reset_recalls()
    monkeypatch.setattr(settings, "TOOL_RESULT_SPILL_THRESHOLD", 500)
    spill = json.loads(
        call("memory_search", {"query": "budgetmarker", "max_response_chars": 100000})
    )
    assert spill["offloaded"] is True
    with get_db() as conn:
        assert conn.execute("SELECT sum(recall_count) FROM memory_records").fetchone()[
            0
        ] == len(ids)
    fetched = json.loads(
        call("result_fetch", {"handle": spill["handle"], "limit": 50000})
    )
    assert json.loads(fetched["content"])["response_limited"] is False
    with get_db() as conn:
        assert conn.execute("SELECT sum(recall_count) FROM memory_records").fetchone()[
            0
        ] == len(ids)


def test_mcp_default_budget_delivers_inline_instead_of_offloading(
    search, test_client, agent_token
):
    """Without a budget, an oversized search used to be offloaded behind a handle.

    Now it is trimmed to whole records under the offload threshold, so the agent
    sees the best matches inline, and recall counts only what was delivered.
    """
    request, ids = search
    from app.config import settings

    full = request(max_response_chars=100000).json()["data"]
    assert _size(full) > settings.TOOL_RESULT_SPILL_THRESHOLD
    _reset_recalls()
    response = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": {"query": "budgetmarker"}},
        },
    )
    text = response.json()["result"]["content"][0]["text"]
    data = json.loads(text)
    assert "offloaded" not in data
    assert len(text) <= settings.TOOL_RESULT_SPILL_THRESHOLD
    assert data["response_limited"] is True
    assert 0 < len(data["records"]) < len(ids)
    assert data["records"] == full["records"][: len(data["records"])]
    with get_db() as conn:
        recalled = conn.execute("SELECT sum(recall_count) FROM memory_records").fetchone()[0]
    assert recalled == len(data["records"])


def test_manifest_exposes_budget_and_pagination(search, test_client, agent_token):
    response = test_client.get(
        "/mcp", headers={"Authorization": f"Bearer {agent_token}"}
    )
    tool = next(t for t in response.json()["tools"] if t["name"] == "memory_search")
    props = tool["inputSchema"]["properties"]
    assert "max_response_chars" in props
    assert "offset" in props
