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


def test_contract_fingerprint_ignores_descriptions_and_tool_order():
    expected = [
        {
            "name": "read",
            "description": "Read a record",
            "inputSchema": {
                "type": "object",
                "description": "Input",
                "properties": {"id": {"type": "string", "description": "ID"}},
                "required": ["id"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {"name": "write", "inputSchema": {"type": "object"}},
    ]
    equivalent = [
        {"name": "write", "description": "Changed copy", "input_schema": {"type": "object"}},
        {
            "name": "read",
            "description": "Different words",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"description": "Different ID copy", "type": "string"}},
                "required": ["id"],
            },
            "raw": {"annotations": {"readOnlyHint": True, "title": "display only"}},
        },
    ]
    assert connector_service.mcp_tool_contract_fingerprint(expected) == (
        connector_service.mcp_tool_contract_fingerprint(equivalent)
    )


def test_contract_fingerprint_rejects_schema_or_authority_annotation_change():
    base = [{"name": "read", "inputSchema": {"type": "object"}}]
    changed_schema = [{"name": "read", "inputSchema": {"type": "string"}}]
    changed_annotation = [
        {
            "name": "read",
            "inputSchema": {"type": "object"},
            "annotations": {"destructiveHint": True},
        }
    ]
    base_fingerprint = connector_service.mcp_tool_contract_fingerprint(base)
    assert base_fingerprint != connector_service.mcp_tool_contract_fingerprint(changed_schema)
    assert base_fingerprint != connector_service.mcp_tool_contract_fingerprint(changed_annotation)
