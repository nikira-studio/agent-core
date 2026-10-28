"""MCP memory tools share the per-agent rate limits REST applies (docs/security.md)."""

from app.database import get_db
from app.security.rate_limiter import ConcurrentSearchGuard, RateLimitConfig, RateLimiter
from app.services import agent_service, workspace_service

SCOPE = "workspace:rate-test"


def _agent():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("rate-test", "Rate Test", owner_id)
    _, key = agent_service.create_agent(
        agent_id="rate-agent", display_name="Rate Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {key}"}


def _search(client, headers):
    return client.post("/mcp", headers=headers,
                       json={"tool": "memory_search", "params": {"query": "anything", "scope": SCOPE}})


def test_mcp_memory_search_is_rate_limited_and_shares_the_rest_budget(test_client, admin_token, monkeypatch):
    headers = _agent()
    limit = 4  # one token an hour, so a slow test run cannot earn tokens back
    monkeypatch.setitem(RateLimiter._limits, ("memory_search", "agent"), RateLimitConfig(limit, 1 / 3600))

    # Half the budget over REST, the rest over MCP: one bucket for both.
    for _ in range(limit // 2):
        assert test_client.post("/api/memory/search", headers=headers,
                                json={"query": "anything", "scope": SCOPE}).status_code == 200
    for _ in range(limit - limit // 2):
        assert _search(test_client, headers).status_code == 200

    refused = _search(test_client, headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert refused.headers["X-RateLimit-Remaining"] == "0"


def test_mcp_memory_search_respects_the_concurrent_search_limit(test_client, admin_token, monkeypatch):
    headers = _agent()
    monkeypatch.setattr(ConcurrentSearchGuard, "acquire", classmethod(lambda cls, agent_id: False))
    refused = _search(test_client, headers)
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "CONCURRENT_LIMIT"
