import json

from app.services import connector_service, mcp_provider_service


def test_effective_endpoint_prefers_binding_override():
    assert connector_service.effective_mcp_endpoint(
        {"endpoint_url_override": "https://instance.example/mcp"},
        {"endpoint_url": "https://default.example/mcp"},
    ) == "https://instance.example/mcp"
    assert connector_service.effective_mcp_endpoint(
        {"endpoint_url_override": None}, {"endpoint_url": "https://default.example/mcp"}
    ) == "https://default.example/mcp"


def test_override_requires_matching_normalized_tool_contract(clean_db, monkeypatch):
    tools = [{"name": "read", "inputSchema": {"type": "object"}}]
    connector_service.create_connector_type(
        connector_type_id="native",
        display_name="Native",
        auth_type="none",
        supported_actions=["read"],
        provider_type="mcp",
        endpoint_url="https://default.example/mcp",
        tool_snapshot_json=json.dumps({"tools": tools}),
    )
    monkeypatch.setattr(mcp_provider_service, "validate_mcp_server_url", lambda value: value)
    monkeypatch.setattr(mcp_provider_service, "discover_all_tools", lambda *a, **k: tools)
    binding = connector_service.create_binding(
        "native", "Instance", "user:u", endpoint_url_override="https://instance.example/mcp"
    )
    assert binding["endpoint_url_override"] == "https://instance.example/mcp"

    monkeypatch.setattr(
        mcp_provider_service, "discover_all_tools",
        lambda *a, **k: [{"name": "write", "inputSchema": {"type": "object"}}],
    )
    try:
        connector_service.create_binding(
            "native", "Mismatch", "user:u", endpoint_url_override="https://other.example/mcp"
        )
    except ValueError as exc:
        assert "separate connector type" in str(exc)
    else:
        raise AssertionError("mismatching endpoint contract was accepted")
