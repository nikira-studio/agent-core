import pytest


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

    def test_connectors_list_returns_three_types(self, test_client, admin_token):
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
        assert "github" in type_ids
        assert "slack" in type_ids
        assert "generic_http" in type_ids

    def test_connectors_actions_list(self, test_client, admin_token):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_actions_list",
                "params": {"connector_type_id": "github"},
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["connector_type_id"] == "github"
        assert "create_issue" in result["data"]["actions"]
        assert "comment_issue" in result["data"]["actions"]
        assert "read_repo" in result["data"]["actions"]


class TestConnectorBindingWorkflow:
    def test_create_binding_and_list(self, test_client, admin_token):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-token",
                "value": "ghp_faketoken123",
            },
        )
        assert vault_r.status_code == 201
        vault_entry = vault_r.json()["data"]["entry"]

        r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "test-gh-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
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

    def test_binding_test_with_bad_token_fails_readably(self, test_client, admin_token):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"scope": "workspace:test", "name": "bad-gh", "value": "ghp_badtoken"},
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "bad-gh-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "tool": "connectors_bindings_test",
                "params": {"binding_id": binding["id"]},
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["success"] is False
        assert "error" in result["data"]


class TestConnectorRunEndToEnd:
    def test_run_read_repo_with_fake_token_returns_proper_error(
        self, test_client, admin_token
    ):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-readme",
                "value": "ghp_faketoken",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-readme-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
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
                    "action": "read_repo",
                    "params": {"owner": "torvalds", "repo": "linux"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["success"] is False
        assert "error" in result["data"]

    def test_run_with_invalid_action_rejected(self, test_client, admin_token):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-invalid",
                "value": "ghp_faketoken",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-invalid-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
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
                "connector_type_id": "github",
                "name": "gh-no-cred",
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
                    "action": "read_repo",
                    "params": {"owner": "torvalds", "repo": "linux"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert "NO_CREDENTIAL" in result["error"]["code"]

    def test_raw_credential_never_in_response(self, test_client, admin_token):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-secret",
                "value": "SUPERSECRET123TOKEN",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-secret-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
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
                    "action": "read_repo",
                    "params": {"owner": "torvalds", "repo": "linux"},
                },
            },
        )
        assert r.status_code == 200
        response_text = r.text
        assert "SUPERSECRET123TOKEN" not in response_text
        assert "ghp_faketoken" not in response_text

    def test_run_read_repo_success_with_mock(self, test_client, admin_token):
        from unittest.mock import patch

        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-success",
                "value": "ghp_testtoken",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-success-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
            },
        )
        binding = binding_r.json()["data"]["binding"]

        mock_response = {
            "full_name": "test/repo",
            "description": "A test repository",
            "stargazers_count": 42,
            "forks_count": 10,
            "language": "Python",
            "open_issues_count": 5,
            "created_at": "2024-01-01T00:00:00Z",
            "pushed_at": "2024-06-01T00:00:00Z",
        }

        with patch(
            "app.connectors.github.GitHubConnector._do",
            return_value=(200, mock_response),
        ):
            r = test_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "tool": "connectors_run",
                    "params": {
                        "binding_id": binding["id"],
                        "action": "read_repo",
                        "params": {"owner": "test", "repo": "repo"},
                    },
                },
            )

        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True
        assert result["data"]["success"] is True
        assert result["data"]["repo"]["full_name"] == "test/repo"
        assert result["data"]["repo"]["stars"] == 42
        assert result["data"]["repo"]["language"] == "Python"

        from app.services import connector_service

        executions = connector_service.list_executions(binding["id"])
        assert len(executions) >= 1
        success_execs = [e for e in executions if e["result_status"] == "success"]
        assert len(success_execs) >= 1
        last_exec = success_execs[0]
        assert last_exec["action"] == "read_repo"


class TestConnectorScopeEnforcement:
    def test_agent_cannot_run_binding_outside_read_scopes(
        self, test_client, admin_token, agent_token
    ):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:other-ws",
                "name": "gh-other",
                "value": "ghp_faketoken",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-other-binding",
                "scope": "workspace:other-ws",
                "credential_id": vault_entry["id"],
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
                    "action": "read_repo",
                    "params": {"owner": "torvalds", "repo": "linux"},
                },
            },
        )
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is False
        assert "SCOPE_DENIED" in result["error"]["code"]


class TestConnectorExecutionLogging:
    def test_execution_is_logged(self, test_client, admin_token):
        vault_r = test_client.post(
            "/api/vault/entries",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "scope": "workspace:test",
                "name": "gh-log",
                "value": "ghp_faketoken",
            },
        )
        vault_entry = vault_r.json()["data"]["entry"]

        binding_r = test_client.post(
            "/api/connector-bindings",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "connector_type_id": "github",
                "name": "gh-log-binding",
                "scope": "workspace:test",
                "credential_id": vault_entry["id"],
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
                    "action": "read_repo",
                    "params": {"owner": "torvalds", "repo": "linux"},
                },
            },
        )
        assert r.status_code == 200

        from app.services import connector_service

        executions = connector_service.list_executions(binding["id"])
        assert len(executions) >= 1
        last_exec = executions[0]
        assert last_exec["action"] == "read_repo"
        assert last_exec["result_status"] == "failure"
        assert last_exec["duration_ms"] is not None
