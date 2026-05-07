import pytest


class TestConnectorSchema:
    def test_connector_tables_exist(self, clean_db):
        from app.database import get_db

        with get_db() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [t["name"] for t in tables]
            assert "connector_types" in table_names
            assert "connector_bindings" in table_names
            assert "connector_executions" in table_names

    def test_connector_types_seeded(self, clean_db):
        from app.services import connector_service

        types = connector_service.list_connector_types()
        type_ids = [t["id"] for t in types]
        assert "github" in type_ids
        assert "slack" in type_ids
        assert "generic_http" in type_ids

    def test_connector_type_fields(self, clean_db):
        from app.services import connector_service

        github = connector_service.get_connector_type("github")
        assert github is not None
        assert github["display_name"] == "GitHub"
        assert github["auth_type"] == "bearer"
        assert "create_issue" in github["supported_actions"]
        assert "comment_issue" in github["supported_actions"]
        assert "read_repo" in github["supported_actions"]
        assert "token" in github["required_credential_fields"]
        assert github["default_binding_rules"] == {"scope": "workspace"}

    def test_slack_connector_type(self, clean_db):
        from app.services import connector_service

        slack = connector_service.get_connector_type("slack")
        assert slack is not None
        assert slack["auth_type"] == "bearer"
        assert "post_message" in slack["supported_actions"]
        assert "list_channels" in slack["supported_actions"]

    def test_generic_http_connector_type(self, clean_db):
        from app.services import connector_service

        gh = connector_service.get_connector_type("generic_http")
        assert gh is not None
        assert gh["auth_type"] == "api_key"
        assert "call_endpoint" in gh["supported_actions"]


class TestConnectorBindingCRUD:
    def test_create_binding(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="test-github",
            scope="workspace:test",
            created_by="user:test",
        )
        assert binding is not None
        assert binding["id"] is not None
        assert binding["connector_type_id"] == "github"
        assert binding["name"] == "test-github"
        assert binding["scope"] == "workspace:test"
        assert binding["enabled"] is True

    def test_get_binding(self, clean_db):
        from app.services import connector_service

        created = connector_service.create_binding(
            connector_type_id="slack",
            name="test-slack",
            scope="user:test",
        )
        fetched = connector_service.get_binding(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["connector_display_name"] == "Slack"

    def test_list_bindings_by_scope(self, clean_db):
        from app.services import connector_service

        connector_service.create_binding("github", "gh1", scope="workspace:ws1")
        connector_service.create_binding("github", "gh2", scope="workspace:ws2")
        connector_service.create_binding("slack", "sl1", scope="workspace:ws1")
        bindings = connector_service.list_bindings(scope="workspace:ws1")
        assert len(bindings) == 2

    def test_list_bindings_by_connector_type(self, clean_db):
        from app.services import connector_service

        connector_service.create_binding("github", "gh1", scope="workspace:ws1")
        connector_service.create_binding("slack", "sl1", scope="workspace:ws1")
        bindings = connector_service.list_bindings(connector_type_id="github")
        assert len(bindings) == 1
        assert bindings[0]["connector_type_id"] == "github"

    def test_update_binding(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="original-name",
            scope="workspace:test",
        )
        ok = connector_service.update_binding(
            binding["id"],
            name="updated-name",
            enabled=False,
        )
        assert ok is True
        updated = connector_service.get_binding(binding["id"])
        assert updated["name"] == "updated-name"
        assert updated["enabled"] is False

    def test_delete_binding(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="to-delete",
            scope="workspace:test",
        )
        ok = connector_service.delete_binding(binding["id"])
        assert ok is True
        assert connector_service.get_binding(binding["id"]) is None

    def test_binding_scope_normalization(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="scope-test",
            scope="WORKSPACE: MyWorkspace ",
        )
        assert binding["scope"] == "workspace:myworkspace"


class TestConnectorExecutions:
    def test_log_execution(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="exec-test",
            scope="workspace:test",
        )
        exec_id = connector_service.log_execution(
            binding_id=binding["id"],
            action="create_issue",
            params_json='{"title": "test"}',
            result_status="success",
            result_body_json='{"id": 123}',
            duration_ms=150,
        )
        assert exec_id is not None
        executions = connector_service.list_executions(binding["id"])
        assert len(executions) == 1
        assert executions[0]["action"] == "create_issue"
        assert executions[0]["result_status"] == "success"

    def test_log_execution_failure(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="fail-test",
            scope="workspace:test",
        )
        exec_id = connector_service.log_execution(
            binding_id=binding["id"],
            action="create_issue",
            params_json='{"title": "bad"}',
            result_status="failure",
            error_message="API rate limit exceeded",
        )
        executions = connector_service.list_executions(binding["id"])
        assert executions[0]["result_status"] == "failure"
        assert "rate limit" in executions[0]["error_message"]


class TestVaultStillWorks:
    def test_vault_entry_still_works(self, clean_db):
        from app.services import vault_service

        entry = vault_service.create_vault_entry(
            scope="workspace:test",
            name="api-key",
            value_plaintext="secret123",
            value_type="api",
        )
        assert entry["reference_name"].startswith("AC_SECRET_")
        fetched = vault_service.get_vault_entry(entry["id"])
        assert fetched["name"] == "api-key"
        entries = vault_service.list_vault_entries(scope="workspace:test")
        assert len(entries) == 1
        assert entries[0]["id"] == entry["id"]


class TestBindingWithVaultCredential:
    def test_binding_links_to_vault_entry(self, clean_db):
        from app.services import vault_service, connector_service

        vault_entry = vault_service.create_vault_entry(
            scope="workspace:test",
            name="gh-token",
            value_plaintext="ghp_testtoken123",
            value_type="api",
        )
        binding = connector_service.create_binding(
            connector_type_id="github",
            name="gh-with-vault",
            scope="workspace:test",
            credential_id=vault_entry["id"],
        )
        assert binding["credential_id"] == vault_entry["id"]
        binding_with_cred = connector_service.get_binding_with_credential(binding["id"])
        assert binding_with_cred["credential_plaintext"] == "ghp_testtoken123"
        assert (
            binding_with_cred["vault_entry"]["reference_name"]
            == vault_entry["reference_name"]
        )

    def test_binding_without_credential(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="github",
            name="no-cred",
            scope="workspace:test",
        )
        binding_with_cred = connector_service.get_binding_with_credential(binding["id"])
        assert binding_with_cred["credential_plaintext"] is None
