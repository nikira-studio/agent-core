import json

import httpx
import pytest

from app.services import mcp_provider_service as m


def _session_enforcing_handler():
    """Mimic a Streamable-HTTP MCP server (like Context7) that issues a session id
    on initialize and rejects any later request that omits it with HTTP 400."""
    sessions = set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        sid = request.headers.get("mcp-session-id")
        if method == "initialize":
            new_sid = "sess-abc"
            sessions.add(new_sid)
            return httpx.Response(
                200,
                headers={"mcp-session-id": new_sid},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "MockMCP"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if not sid or sid not in sessions:
            return httpx.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "No valid session ID provided"},
                    "id": None,
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "do_thing"}]}},
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
                },
            )
        return httpx.Response(404)

    return handler


@pytest.fixture
def mock_mcp(monkeypatch):
    transport = httpx.MockTransport(_session_enforcing_handler())
    original = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(m.httpx, "Client", factory)


URL = "https://mock.example.com/mcp"


def test_discover_all_tools_establishes_session(mock_mcp):
    # Without the initialize handshake + session propagation this 400s.
    tools = m.discover_all_tools(URL, validate_url=False)
    assert [t["name"] for t in tools] == ["do_thing"]


def test_execute_mcp_tool_establishes_session(mock_mcp):
    result = m.execute_mcp_tool(URL, "do_thing", {}, transport_type="streamable_http")
    assert result.success is True
    assert result.body["content"][0]["text"] == "ok"


def test_discover_mcp_server_maps_transport_error_to_valueerror(monkeypatch):
    # An unreachable target must surface as a ValueError (→ 4xx at the route),
    # not propagate as a raw httpx error that the API turns into a 500.
    monkeypatch.setattr(m, "validate_mcp_server_url", lambda u: u.rstrip("/"))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(m.httpx, "Client", factory)

    with pytest.raises(ValueError, match="Could not reach MCP server"):
        m.discover_mcp_server(URL)
