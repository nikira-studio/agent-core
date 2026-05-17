

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
        assert "generic_http" in type_ids

    def test_generic_http_connector_type(self, clean_db):
        from app.services import connector_service

        gh = connector_service.get_connector_type("generic_http")
        assert gh is not None
        assert gh["display_name"] == "Generic HTTP API"
        assert gh["provider_type"] == "builtin"
        assert gh["auth_type"] == "api_key"
        assert "call_endpoint" in gh["supported_actions"]
        assert "token" in gh["required_credential_fields"]

    def test_connector_type_metadata_columns_exist(self, clean_db):
        from app.database import get_db

        with get_db() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
            }
        assert {"provider_type", "endpoint_url", "transport_type", "capabilities_json", "tool_snapshot_json"}.issubset(
            columns
        )

    def test_imported_connector_defaults_to_openapi_provider(self, clean_db):
        from app.services import connector_service

        ct = connector_service.create_connector_type(
            connector_type_id="example-api",
            display_name="Example API",
            supported_actions=["call_endpoint"],
        )
        assert ct["provider_type"] == "openapi"

    def test_generic_http_seed_does_not_reappear_when_catalog_already_has_types(
        self, clean_db
    ):
        from app.database import init_db
        from app.database import get_db
        from app.services import connector_service
        import json

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO connector_types
                (id, display_name, auth_type, supported_actions_json,
                 required_credential_fields_json, disabled_actions_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "custom_http",
                    "Custom HTTP",
                    "none",
                    json.dumps(["call_endpoint"]),
                    json.dumps([]),
                    json.dumps([]),
                ),
            )
            conn.commit()

        assert connector_service.delete_connector_type("generic_http") is True

        init_db()

        assert connector_service.get_connector_type("generic_http") is None
        assert connector_service.get_connector_type("custom_http") is not None


class TestConnectorBindingCRUD:
    def test_create_binding(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="test-generic",
            scope="workspace:test",
            created_by="user:test",
        )
        assert binding is not None
        assert binding["id"] is not None
        assert binding["connector_type_id"] == "generic_http"
        assert binding["name"] == "test-generic"
        assert binding["scope"] == "workspace:test"
        assert binding["enabled"] is True

    def test_get_binding(self, clean_db):
        from app.services import connector_service

        created = connector_service.create_binding(
            connector_type_id="generic_http",
            name="test-binding",
            scope="user:test",
        )
        fetched = connector_service.get_binding(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["connector_display_name"] == "Generic HTTP API"

    def test_list_bindings_by_scope(self, clean_db):
        from app.services import connector_service

        connector_service.create_binding("generic_http", "gh1", scope="workspace:ws1")
        connector_service.create_binding("generic_http", "gh2", scope="workspace:ws2")
        connector_service.create_binding("generic_http", "gh3", scope="workspace:ws1")
        bindings = connector_service.list_bindings(scope="workspace:ws1")
        assert len(bindings) == 2

    def test_list_bindings_by_connector_type(self, clean_db):
        from app.services import connector_service

        connector_service.create_binding("generic_http", "gh1", scope="workspace:ws1")
        bindings = connector_service.list_bindings(connector_type_id="generic_http")
        assert len(bindings) == 1
        assert bindings[0]["connector_type_id"] == "generic_http"

    def test_update_binding(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
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
            connector_type_id="generic_http",
            name="to-delete",
            scope="workspace:test",
        )
        ok = connector_service.delete_binding(binding["id"])
        assert ok is True
        assert connector_service.get_binding(binding["id"]) is None

    def test_binding_scope_normalization(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="scope-test",
            scope="WORKSPACE: MyWorkspace ",
        )
        assert binding["scope"] == "workspace:myworkspace"


class TestConnectorExecutions:
    def test_log_execution(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="exec-test",
            scope="workspace:test",
        )
        connector_service.log_execution(
            binding_id=binding["id"],
            action="call_endpoint",
            params_json='{"path": "/status"}',
            result_status="success",
            result_body_json='{"ok": true}',
            duration_ms=150,
        )
        executions = connector_service.list_executions(binding["id"])
        assert len(executions) == 1
        assert executions[0]["action"] == "call_endpoint"
        assert executions[0]["result_status"] == "success"

    def test_log_execution_failure(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="fail-test",
            scope="workspace:test",
        )
        connector_service.log_execution(
            binding_id=binding["id"],
            action="call_endpoint",
            params_json='{"path": "/bad"}',
            result_status="failure",
            error_message="Connection refused",
        )
        executions = connector_service.list_executions(binding["id"])
        assert executions[0]["result_status"] == "failure"
        assert "refused" in executions[0]["error_message"]


class TestCredentialStillWorks:
    def test_credential_still_works(self, clean_db):
        from app.services import credential_service, connector_service

        credential_entry = credential_service.create_credential(
            scope="workspace:test",
            name="generic-token",
            value_plaintext="test-secret-123",
        )
        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="binding-with-credential",
            scope="workspace:test",
            credential_id=credential_entry["id"],
        )
        assert binding["credential_id"] == credential_entry["id"]
        binding_with_cred = connector_service.get_binding_with_credential(binding["id"])
        assert binding_with_cred["credential_plaintext"] == "test-secret-123"
        assert (
            binding_with_cred["credential"]["reference_name"]
            == credential_entry["reference_name"]
        )

    def test_binding_without_credential(self, clean_db):
        from app.services import connector_service

        binding = connector_service.create_binding(
            connector_type_id="generic_http",
            name="no-cred",
            scope="workspace:test",
        )
        binding_with_cred = connector_service.get_binding_with_credential(binding["id"])
        assert binding_with_cred["credential_plaintext"] is None
