import json
from pathlib import Path


def test_securo_adapter_installs_as_a_native_mcp_connector(clean_db, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import adapter_loader, connector_service

    monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
    installed = adapter_loader.install_adapter("securo")
    connector_type = installed["connector_type"]

    assert connector_type["provider_type"] == "mcp"
    assert connector_type["backend_type"] == "mcp"
    assert connector_type["transport_type"] == "streamable_http"
    assert connector_type["required_credential_fields"] == ["api_key"]
    tools = json.loads(connector_type["tool_snapshot_json"])["tools"]
    assert {tool["name"] for tool in tools} >= {"list_accounts", "list_transactions"}

    binding = connector_service.create_binding(
        connector_type_id="securo",
        name="Securo",
        scope="user:admin",
        config_json=json.dumps({"base_url": "https://securo.example.com/mcp"}),
        created_by="admin",
    )
    assert connector_service.effective_mcp_endpoint(binding, connector_type) == "https://securo.example.com/mcp"
    assert Path(tmp_path, "adapters", "securo", "adapter.json").exists()


def test_securo_adapter_contract_checks_tool_names_not_schema(clean_db, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import adapter_loader, connector_service

    monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
    connector_type = adapter_loader.install_adapter("securo")["connector_type"]
    expected = json.loads(connector_type["tool_snapshot_json"])["tools"]
    actual = [
        {
            "name": tool["name"],
            "inputSchema": {"type": "object", "properties": {"new": {"type": "string"}}},
        }
        for tool in expected
    ]

    connector_service.validate_mcp_binding_contract({}, connector_type, tools=actual)


def test_securo_adapter_executes_with_binding_url_and_credential(
    clean_db, tmp_path, monkeypatch
):
    from app.config import settings
    from app.services import (
        adapter_loader,
        connector_service,
        credential_service,
        mcp_provider_service,
    )

    monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
    adapter_loader.install_adapter("securo")
    credential = credential_service.create_credential(
        scope="user:admin",
        name="securo-token",
        value_plaintext="securo-secret",
    )
    binding = connector_service.create_binding(
        connector_type_id="securo",
        name="Securo",
        scope="user:admin",
        credential_id=credential["id"],
        config_json=json.dumps({"base_url": "https://securo.example.com/mcp"}),
        created_by="admin",
    )
    monkeypatch.setattr(
        mcp_provider_service, "validate_mcp_server_url", lambda value: value
    )

    def execute(endpoint_url, action, params, credential, config_json, transport_type):
        assert endpoint_url == "https://securo.example.com/mcp"
        assert action == "list_accounts"
        assert params == {}
        assert credential == "securo-secret"
        assert json.loads(config_json)["base_url"] == endpoint_url
        assert transport_type == "streamable_http"
        return mcp_provider_service.MCPExecutionResult(
            success=True, body={"accounts": []}, status=200
        )

    monkeypatch.setattr(mcp_provider_service, "execute_mcp_tool", execute)
    result = connector_service.execute_binding_action(
        binding["id"], "list_accounts", {}
    )
    assert result["success"] is True
    assert result["body"] == {"accounts": []}
