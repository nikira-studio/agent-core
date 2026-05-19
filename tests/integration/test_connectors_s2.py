
import json


class TestConnectorMCPTools:
    def test_connectors_list_returns_seeded_types(self, test_client, admin_token):
        r = test_client.get(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        manifest = r.json()
        tool_names = [t["name"] for t in manifest["tools"]]
        assert "connectors_list" in tool_names
        assert "connectors_run" in tool_names
        assert "connectors_bindings_list" in tool_names
        assert "connectors_bindings_test" in tool_names
        assert "connectors_actions_list" in tool_names

    def test_connectors_list_jsonrpc(self, test_client, admin_token):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "connectors_list", "arguments": {}},
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["result"]["content"][0]["text"].startswith("{")

    def test_connectors_list_returns_generic_http(self, test_client, admin_token):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"tool": "connectors_list", "params": {}},
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        connectors = result["data"]["connectors"]
        type_ids = [c["id"] for c in connectors]
        assert "generic_http" in type_ids

    def test_connectors_actions_list(self, test_client, admin_token):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_actions_list",
                "params": {"connector_type_id": "generic_http"},
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["connector_type_id"] == "generic_http"
        assert "call_endpoint" in result["data"]["actions"]

    def test_disabled_actions_are_hidden_from_tools_by_default(self, test_client, admin_token):
        disable_r = test_client.put(
            "/api/connector-types/generic_http/actions",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"disabled_actions": ["call_endpoint"]},
        )
        assert disable_r.status_code == 200, disable_r.text

        tools_r = test_client.get(
            "/api/connector-types/generic_http/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert tools_r.status_code == 200, tools_r.text
        tools = tools_r.json()["data"]["tools"]
        assert all(t["action"] != "call_endpoint" for t in tools)

        tools_all_r = test_client.get(
            "/api/connector-types/generic_http/tools?include_disabled=1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert tools_all_r.status_code == 200, tools_all_r.text
        tools_all = tools_all_r.json()["data"]["tools"]
        call_endpoint = next(t for t in tools_all if t["action"] == "call_endpoint")
        assert call_endpoint["enabled"] is False

    def test_connector_type_tools_endpoint(self, test_client, admin_token):
        r = test_client.get(
            "/api/connector-types/generic_http/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        tool_names = [t["name"] for t in result["data"]["tools"]]
        assert "call_endpoint" in tool_names

    def test_binding_tools_endpoint_returns_mcp_tools(self, test_client, admin_token, monkeypatch):
        from types import SimpleNamespace
        from app.services import connector_service, mcp_provider_service

        connector_service.create_connector_type(
            connector_type_id="binding-mcp",
            display_name="Binding MCP",
            provider_type="mcp",
            auth_type="none",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape","description":"Scrape a page"}]}',
        )
        binding = connector_service.create_binding(
            connector_type_id="binding-mcp",
            name="binding-mcp",
            scope="workspace:test",
        )

        r = test_client.get(
            f"/api/connector-bindings/{binding['id']}/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert [t["name"] for t in data["tools"]] == ["scrape"]

    def test_test_binding_runs_mcp_probe(self, test_client, admin_token, monkeypatch):
        from app.services import connector_service

        connector_service.create_connector_type(
            connector_type_id="binding-test-mcp",
            display_name="Binding Test MCP",
            provider_type="mcp",
            auth_type="none",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape"}]}',
        )
        binding = connector_service.create_binding(
            connector_type_id="binding-test-mcp",
            name="binding-test-mcp",
            scope="workspace:test",
        )

        def fake_discover(endpoint_url, timeout_ms=10000, headers=None, client=None, validate_url=True):
            assert endpoint_url == "https://example.com/mcp"
            assert timeout_ms <= 10000
            return [{"name": "scrape"}]

        monkeypatch.setattr(
            "app.services.mcp_provider_service.discover_all_tools",
            fake_discover,
        )

        result = connector_service.test_binding(binding["id"])
        assert result["success"] is True
        assert result["tools_discovered"] == 1
        assert result["transport"] == "streamable_http"

    def test_test_binding_without_credential_still_probes_connector(
        self, test_client, admin_token, monkeypatch
    ):
        from app.services import connector_service

        connector_service.create_connector_type(
            connector_type_id="binding-no-cred-mcp",
            display_name="Binding No Cred MCP",
            provider_type="mcp",
            auth_type="api_key",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape"}]}',
        )
        binding = connector_service.create_binding(
            connector_type_id="binding-no-cred-mcp",
            name="binding-no-cred-mcp",
            scope="workspace:test",
        )

        called = {}

        def fake_discover(endpoint_url, timeout_ms=10000, headers=None, client=None, validate_url=True):
            called["endpoint_url"] = endpoint_url
            called["timeout_ms"] = timeout_ms
            called["headers"] = headers
            return [{"name": "scrape"}]

        monkeypatch.setattr(
            "app.services.mcp_provider_service.discover_all_tools",
            fake_discover,
        )
        monkeypatch.setattr(
            "app.services.connector_service.get_binding_with_credential",
            lambda binding_id: {
                **connector_service.get_binding(binding_id),
                "credential_plaintext": None,
            },
        )

        result = connector_service.test_binding(binding["id"])
        assert result["success"] is True
        assert called["endpoint_url"] == "https://example.com/mcp"
        assert called["headers"] is not None
        assert result["transport"] == "streamable_http"

    def test_import_mcp_server_rejects_unsupported_transport(self, test_client, admin_token):
        r = test_client.post(
            "/api/connector-types/import-mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "url": "https://example.test/mcp",
                "display_name": "Bad Transport",
                "transport_type": "sse",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_REQUEST"

    def test_import_mcp_server_and_list_tools(self, test_client, admin_token, monkeypatch):
        from types import SimpleNamespace
        from app.services import mcp_provider_service

        def fake_discover(endpoint_url, timeout_ms=60000, headers=None):
            assert endpoint_url == "https://example.com/mcp"
            return SimpleNamespace(
                server_name="Firecrawl",
                protocol_version="2024-11-05",
                capabilities={"tools": True},
                tools=[
                    {
                        "name": "scrape",
                        "description": "Scrape a page",
                        "input_schema": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}},
                        },
                    },
                    {
                        "name": "crawl",
                        "description": "Crawl a site",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                ],
            )

        monkeypatch.setattr(mcp_provider_service, "discover_mcp_server", fake_discover)

        r = test_client.post(
            "/api/connector-types/import-mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "url": "https://example.com/mcp",
                "display_name": "Firecrawl",
                "transport_type": "streamable_http",
            },
        )
        assert r.status_code == 201, r.json()
        data = r.json()["data"]
        ct = data["connector_type"]
        assert ct["provider_type"] == "mcp"
        assert ct["endpoint_url"] == "https://example.com/mcp"
        assert data["tool_count"] == 2

        tools_r = test_client.get(
            f"/api/connector-types/{ct['id']}/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert tools_r.status_code == 200
        tools = tools_r.json()["data"]["tools"]
        assert [t["name"] for t in tools] == ["scrape", "crawl"]

        mcp_r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_actions_list",
                "params": {"connector_type_id": ct["id"]},
            },
        )
        assert mcp_r.status_code == 200
        result = mcp_r.json()
        assert result["ok"] is True
        assert result["data"]["actions"] == ["scrape", "crawl"]

    def test_preview_openapi_spec_returns_summary_without_persisting(self, test_client, admin_token, monkeypatch):
        from app.services import connector_service
        from app.services import openapi_service

        monkeypatch.setattr(openapi_service, "validate_public_url", lambda url: None)

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Preview API", "version": "1.0.0"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "items_list",
                        "summary": "List items",
                        "tags": ["items"],
                    }
                }
            },
        }

        r = test_client.post(
            "/api/connector-types/preview",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"spec_json": json.dumps(spec), "display_name": "Preview API"},
        )
        assert r.status_code == 200, r.json()
        data = r.json()["data"]["preview"]
        assert data["display_name"] == "Preview API"
        assert data["operation_count"] == 1
        assert data["supported_actions"] == ["items_list"]
        assert connector_service.get_connector_type("preview-api") is None

    def test_import_mcp_server_rejects_private_urls(self, test_client, admin_token):
        r = test_client.post(
            "/api/connector-types/import-mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "url": "http://127.0.0.1:3002/mcp",
                "display_name": "Bad MCP",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "IMPORT_FAILED"
        assert "Blocked" in r.json()["error"]["message"]

    def test_import_mcp_server_rejects_credential_id_field(
        self, test_client, admin_token
    ):
        r = test_client.post(
            "/api/connector-types/import-mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "url": "https://example.test/mcp",
                "display_name": "Bad MCP",
                "credential_id": "ignored-for-now",
            },
        )
        assert r.status_code == 422

    def test_refresh_mcp_connector_type(self, test_client, admin_token, monkeypatch):
        from types import SimpleNamespace
        from app.services import connector_service
        from app.services import mcp_provider_service

        connector_service.create_connector_type(
            connector_type_id="local-mcp",
            display_name="Local MCP",
            provider_type="mcp",
            auth_type="none",
            supported_actions=["old_tool"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"old_tool"}]}',
        )

        def fake_discover(endpoint_url, timeout_ms=60000, headers=None):
            assert endpoint_url == "https://example.com/mcp"
            return SimpleNamespace(
                server_name="Local MCP",
                protocol_version="2024-11-05",
                capabilities={"tools": True},
                tools=[{"name": "new_tool", "description": "New tool", "input_schema": {}}],
            )

        monkeypatch.setattr(mcp_provider_service, "discover_mcp_server", fake_discover)

        r = test_client.post(
            "/api/connector-types/local-mcp/refresh",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"timeout_ms": 5000},
        )
        assert r.status_code == 200, r.json()
        data = r.json()["data"]
        assert data["tool_count"] == 1
        assert data["connector_type"]["provider_type"] == "mcp"
        tools_r = test_client.get(
            "/api/connector-types/local-mcp/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert tools_r.status_code == 200
        tools = tools_r.json()["data"]["tools"]
        assert [t["name"] for t in tools] == ["new_tool"]

    def test_run_mcp_tool_via_mcp_and_rest(self, test_client, admin_token, monkeypatch):
        from app.services import connector_service, mcp_provider_service

        connector_service.create_connector_type(
            connector_type_id="firecrawl",
            display_name="Firecrawl",
            provider_type="mcp",
            auth_type="none",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape","description":"Scrape a page"}]}',
        )
        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "firecrawl",
                "name": "firecrawl-binding",
                "scope": "workspace:test",
                "config_json": json.dumps(
                    {"timeout_ms": 5000, "headers": {"X-Test": "1"}}
                ),
            },
        )
        assert binding_r.status_code == 201, binding_r.json()
        binding = binding_r.json()["data"]["binding"]

        def fake_execute(endpoint_url, action, params=None, credential=None, config_json=None, transport_type="streamable_http"):
            assert endpoint_url == "https://example.com/mcp"
            assert action == "scrape"
            assert params == {"url": "https://example.com"}
            assert credential is None
            assert transport_type == "streamable_http"
            cfg = json.loads(config_json)
            assert cfg["timeout_ms"] == 5000
            assert cfg["headers"]["X-Test"] == "1"
            return mcp_provider_service.MCPExecutionResult(
                success=True,
                body={"content": [{"type": "text", "text": "{\"ok\":true}"}]},
                status=200,
                transport=transport_type,
            )

        monkeypatch.setattr(mcp_provider_service, "execute_mcp_tool", fake_execute)

        mcp_r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "scrape",
                    "params": {"url": "https://example.com"},
                },
            },
        )
        assert mcp_r.status_code == 200, mcp_r.json()
        result = mcp_r.json()
        assert result["ok"] is True
        assert result["data"]["success"] is True
        assert result["data"]["body"]["content"][0]["text"] == '{"ok":true}'
        assert result["data"]["transport"] == "streamable_http"

        rest_r = test_client.post(
            f"/api/connector-bindings/{binding['id']}/run",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"action": "scrape", "params": {"url": "https://example.com"}},
        )
        assert rest_r.status_code == 200, rest_r.json()
        rest_result = rest_r.json()["data"]["result"]
        assert rest_result["success"] is True
        assert rest_result["body"]["content"][0]["text"] == '{"ok":true}'

        executions_r = test_client.get(
            f"/api/connector-bindings/{binding['id']}/executions",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert executions_r.status_code == 200
        executions = executions_r.json()["data"]["executions"]
        assert len(executions) >= 2
        assert any(exec_row["action"] == "scrape" for exec_row in executions)

    def test_run_mcp_tool_with_binding_credential(self, test_client, admin_token, monkeypatch):
        from app.services import connector_service, mcp_provider_service

        connector_service.create_connector_type(
            connector_type_id="firecrawl-auth",
            display_name="Firecrawl Auth",
            provider_type="mcp",
            auth_type="bearer",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape","description":"Scrape a page"}]}',
        )
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "firecrawl-token",
                "value": "supersecret",
            },
        )
        assert cred_r.status_code == 201, cred_r.json()
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "firecrawl-auth",
                "name": "firecrawl-auth-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
                "config_json": json.dumps({"timeout_ms": 7000}),
            },
        )
        assert binding_r.status_code == 201, binding_r.json()
        binding = binding_r.json()["data"]["binding"]

        def fake_execute(endpoint_url, action, params=None, credential=None, config_json=None, transport_type="streamable_http"):
            assert endpoint_url == "https://example.com/mcp"
            assert credential == "supersecret"
            cfg = json.loads(config_json)
            assert cfg["timeout_ms"] == 7000
            assert action == "scrape"
            assert params == {"url": "https://example.com"}
            return mcp_provider_service.MCPExecutionResult(
                success=True,
                body={"content": [{"type": "text", "text": "{\"ok\":true}"}]},
                status=200,
                transport=transport_type,
            )

        monkeypatch.setattr(mcp_provider_service, "execute_mcp_tool", fake_execute)

        r = test_client.post(
            f"/api/connector-bindings/{binding['id']}/run",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"action": "scrape", "params": {"url": "https://example.com"}},
        )
        assert r.status_code == 200, r.json()
        result = r.json()["data"]["result"]
        assert result["success"] is True
        assert result["body"]["content"][0]["text"] == '{"ok":true}'

    def test_run_disabled_binding_returns_disabled_code(self, test_client, admin_token):
        from app.services import connector_service

        connector_service.create_connector_type(
            connector_type_id="firecrawl-disabled",
            display_name="Firecrawl Disabled",
            provider_type="mcp",
            auth_type="none",
            supported_actions=["scrape"],
            endpoint_url="https://example.com/mcp",
            transport_type="streamable_http",
            capabilities_json='{"tools":true}',
            tool_snapshot_json='{"tools":[{"name":"scrape"}]}',
        )
        binding = connector_service.create_binding(
            connector_type_id="firecrawl-disabled",
            name="disabled-binding",
            scope="workspace:test",
            enabled=False,
        )

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "scrape",
                    "params": {"url": "https://example.com"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert result["error"]["code"] == "DISABLED"

    def test_connector_type_action_disable_and_restore(
        self, test_client, admin_token
    ):
        disable_r = test_client.put(
            "/api/connector-types/generic_http/actions",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"disabled_actions": ["call_endpoint"]},
        )
        assert disable_r.status_code == 200

        hidden_r = test_client.get(
            "/api/connector-types/generic_http/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert hidden_r.status_code == 200
        hidden_tools = hidden_r.json()["data"]["tools"]
        assert all(t["action"] != "call_endpoint" for t in hidden_tools)

        visible_r = test_client.get(
            "/api/connector-types/generic_http/tools?include_disabled=1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert visible_r.status_code == 200
        visible_tools = visible_r.json()["data"]["tools"]
        call_endpoint = next(
            t for t in visible_tools if t["action"] == "call_endpoint"
        )
        assert call_endpoint["enabled"] is False

        mcp_r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_actions_list",
                "params": {"connector_type_id": "generic_http"},
            },
        )
        assert mcp_r.status_code == 200
        mcp_actions = mcp_r.json()["data"]["actions"]
        assert "call_endpoint" not in mcp_actions

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "disabled-action-binding",
                "scope": "workspace:test",
            },
        )
        assert binding_r.status_code == 201
        binding = binding_r.json()["data"]["binding"]

        run_r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"path": "/status"},
                },
            },
        )
        assert run_r.status_code == 200
        run_result = run_r.json()
        assert run_result["ok"] is False
        assert run_result["error"]["code"] == "DISABLED_ACTION"

        restore_r = test_client.put(
            "/api/connector-types/generic_http/actions",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"disabled_actions": []},
        )
        assert restore_r.status_code == 200

        restored_r = test_client.get(
            "/api/connector-types/generic_http/tools",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert restored_r.status_code == 200
        restored_tools = restored_r.json()["data"]["tools"]
        assert any(t["action"] == "call_endpoint" for t in restored_tools)


class TestConnectorBindingWorkflow:
    def test_create_binding_and_list(self, test_client, admin_token):
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "generic-token",
                "value": "generic-secret",
            },
        )
        assert cred_r.status_code == 201
        credential = cred_r.json()["data"]["entry"]

        r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "test-generic-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
            },
        )
        assert r.status_code == 201
        binding = r.json()["data"]["binding"]

        list_r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"tool": "connectors_bindings_list", "params": {}},
        )
        assert list_r.status_code == 200
        bindings = list_r.json()["data"]["bindings"]
        binding_ids = [b["id"] for b in bindings]
        assert binding["id"] in binding_ids


class TestConnectorRunEndToEnd:
    def test_run_generic_http_success_with_mock(
        self, test_client, admin_token, monkeypatch
    ):
        from app.connectors.generic_http import GenericHttpConnector

        def fake_call(self, credential, config, params):
            assert credential == "generic-secret"
            assert config["base_url"] == "https://example.test"
            assert params["path"] == "/status"
            return {"success": True, "status": 200, "body": {"ok": True}}

        monkeypatch.setattr(GenericHttpConnector, "_call", fake_call)

        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "generic-token",
                "value": "generic-secret",
            },
        )
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
                "config_json": '{"base_url":"https://example.test"}',
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"path": "/status"},
                },
            },
        )
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["success"] is True
        assert result["data"]["body"]["ok"] is True

    def test_run_with_invalid_action_rejected(self, test_client, admin_token):
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "generic-invalid",
                "value": "generic-secret",
            },
        )
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-invalid-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "nonexistent_action",
                    "params": {},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert "INVALID_ACTION" in result["error"]["code"]

    def test_run_without_credential_fails_cleanly(self, test_client, admin_token):
        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-no-cred",
                "scope": "workspace:test",
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"url": "https://example.test/status"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert "NO_CREDENTIAL" in result["error"]["code"]

    def test_raw_credential_never_in_response(self, test_client, admin_token):
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "generic-secret-test",
                "value": "SUPERSECRET123TOKEN",
            },
        )
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-secret-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"url": "https://example.test/status"},
                },
            },
        )
        assert r.status_code == 200
        response_text = r.text
        assert "SUPERSECRET123TOKEN" not in response_text


class TestConnectorScopeEnforcement:
    def test_create_binding_rejects_unreadable_linked_credential(
        self, test_client, clean_db
    ):
        from app.services.auth_service import create_user, create_session
        from app.services.credential_service import create_credential

        create_user(
            "owner", "owner@test.local", "testpassword123", "Owner User", "user"
        )
        create_user(
            "other", "other@test.local", "testpassword123", "Other User", "user"
        )
        other_session = create_session("other")["session_id"]
        credential = create_credential(
            "user:owner",
            "owner-token",
            value_plaintext="owner-secret",
            created_by="owner",
        )

        r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {other_session}"},
            json={
                "connector_type_id": "generic_http",
                "name": "forbidden-binding",
                "scope": "user:other",
                "credential_id": credential["id"],
            },
        )

        assert r.status_code == 403
        assert r.json()["error"]["code"] == "SCOPE_DENIED"

    def test_agent_cannot_run_binding_outside_read_scopes(
        self, test_client, admin_token, agent_token
    ):
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:other-ws",
                "name": "generic-other",
                "value": "generic-secret",
            },
        )
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-other-binding",
                "scope": "workspace:other-ws",
                "credential_id": credential["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"url": "https://example.test/status"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert "SCOPE_DENIED" in result["error"]["code"]


class TestConnectorExecutionLogging:
    def test_execution_is_logged(self, test_client, admin_token):
        cred_r = test_client.post(
            "/api/credentials/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "generic-log",
                "value": "generic-secret",
            },
        )
        credential = cred_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "generic_http",
                "name": "generic-log-binding",
                "scope": "workspace:test",
                "credential_id": credential["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_run",
                "params": {
                    "binding_id": binding["id"],
                    "action": "call_endpoint",
                    "params": {"url": "https://example.test/status"},
                },
            },
        )
        assert r.status_code == 200

        from app.services import connector_service

        executions = connector_service.list_executions(binding["id"])
        assert len(executions) >= 1
        last_exec = executions[0]
        assert last_exec["action"] == "call_endpoint"
        assert last_exec["result_status"] == "failure"
        assert last_exec["duration_ms"] is not None
