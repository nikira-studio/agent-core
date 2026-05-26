"""Integration tests for the Google Gmail adapter manifest."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.connectors.manifest import load_and_validate


class TestGmailAdapterManifest:
    def test_gmail_manifest_loads_valid(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None, f"Expected no error, got: {err}"
        assert m is not None
        assert m.id == "google_gmail"
        assert m.spec_version == "1.0"
        assert m.version == "1.0.0"

    def test_gmail_actions_present(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        action_names = [a["name"] for a in m.actions]
        for action in [
            "send_email",
            "list_messages",
            "get_message",
            "search_messages",
            "modify_labels",
        ]:
            assert action in action_names, f"Missing action: {action}"

    def test_gmail_refresh_block_structure(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        refresh = m.backend.get("refresh", {})
        assert refresh.get("token_url") == "https://oauth2.googleapis.com/token"
        assert refresh.get("grant") == "refresh_token"
        assert "response_map" in refresh
        assert "persist" in refresh
        trigger = refresh.get("trigger", {})
        assert trigger.get("http_status") == 401
        assert trigger.get("or_expired") == "cred.expires_at"

    def test_gmail_oauth2_auth_structure(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        auth = m.backend.get("auth", {})
        assert auth.get("type") == "oauth2"
        apply_spec = auth.get("apply", {})
        assert apply_spec.get("target") == "request_header"
        assert apply_spec.get("name") == "Authorization"
        assert "{{ cred.access_token }}" in apply_spec.get("template", "")


class TestGmailAdapterOAuthFlow:
    def test_token_expiry_triggers_refresh(self):
        """401 response + expired cred triggers AuthExpiredError, then refresh."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "gmail-test-binding"

        with (
            patch.object(svc, "get_binding") as mock_get_binding,
            patch.object(svc, "get_connector_type") as mock_get_ct,
            patch.object(svc, "_validate_action_for_connector") as mock_validate,
            patch.object(svc, "_check_rate_limit") as mock_rate_limit,
            patch.object(svc, "get_binding_with_credential") as mock_get_bwc,
            patch.object(svc, "_resolve_executor") as mock_resolve,
            patch.object(svc, "_build_executor_config") as mock_build_cfg,
            patch.object(sessions, "load_session") as mock_load,
            patch.object(sessions, "save_session") as _mock_save,
            patch.object(sessions, "binding_lock") as mock_lock,
            patch.object(svc.time, "sleep") as _mock_sleep,
        ):
            mock_get_binding.return_value = {
                "id": binding_id,
                "enabled": True,
                "connector_type_id": "google_gmail",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": '{"gmail_base_url": "https://gmail.googleapis.com"}',
            }
            mock_get_ct.return_value = {
                "id": "google_gmail",
                "backend_type": "http",
                "auth_type": "http",
                "backend_json": json.dumps({}),
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None

            cred_obj = MagicMock(
                raw=None,
                fields={
                    "refresh_token": "test-refresh",
                    "client_id": "cid",
                    "client_secret": "csec",
                    "expires_at": "1",
                },
                reference_name="gmail-cref",
            )
            mock_get_bwc.return_value = {
                "credential": cred_obj,
                "credential_plaintext": None,
            }

            execute_count = [0]

            class FakeGmailEngine:
                needs_session = True

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    execute_count[0] += 1
                    from app.connectors.errors import AuthExpiredError

                    if execute_count[0] < 2:
                        raise AuthExpiredError()
                    return {"success": True, "body": {"id": "msg123"}}

                def refresh_session(self, credential, config_json, current_session):
                    return {
                        "session": {
                            "access_token": "refreshed-token",
                            "expires_at": "9999999999",
                        },
                        "credential_update": {"access_token": "refreshed-token"},
                    }

            mock_resolve.return_value = FakeGmailEngine()
            mock_build_cfg.return_value = {}
            mock_load.return_value = None
            mock_lock.return_value = MagicMock(
                __enter__=MagicMock(return_value=None),
                __exit__=MagicMock(return_value=False),
            )

            result = svc.execute_binding_action(binding_id, "send_email")

            assert result["success"] is True, f"Expected success, got: {result}"
            assert execute_count[0] == 2

    def test_concurrent_refresh_race_single_thread(self):
        """Verify that after lock acquisition, session re-read prevents double refresh."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "gmail-race-binding"

        with (
            patch.object(svc, "get_binding") as mock_get_binding,
            patch.object(svc, "get_connector_type") as mock_get_ct,
            patch.object(svc, "_validate_action_for_connector") as mock_validate,
            patch.object(svc, "_check_rate_limit") as mock_rate_limit,
            patch.object(svc, "get_binding_with_credential") as mock_get_bwc,
            patch.object(svc, "_resolve_executor") as mock_resolve,
            patch.object(svc, "_build_executor_config") as mock_build_cfg,
            patch.object(sessions, "load_session") as mock_load,
            patch.object(sessions, "save_session") as mock_save,
            patch.object(sessions, "binding_lock") as mock_lock,
            patch.object(svc.time, "sleep") as _mock_sleep,
        ):
            mock_get_binding.return_value = {
                "id": binding_id,
                "enabled": True,
                "connector_type_id": "google_gmail",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": '{"gmail_base_url": "https://gmail.googleapis.com"}',
            }
            mock_get_ct.return_value = {
                "id": "google_gmail",
                "backend_type": "http",
                "auth_type": "http",
                "backend_json": json.dumps({}),
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None

            cred_obj = MagicMock(
                raw=None,
                fields={
                    "refresh_token": "test-refresh",
                    "client_id": "cid",
                    "client_secret": "csec",
                },
                reference_name="gmail-cref",
            )
            mock_get_bwc.return_value = {
                "credential": cred_obj,
                "credential_plaintext": None,
            }

            refresh_count = [0]

            class FakeGmailEngine:
                needs_session = True

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    from app.connectors.errors import AuthExpiredError

                    raise AuthExpiredError()

                def refresh_session(self, credential, config_json, current_session):
                    refresh_count[0] += 1
                    return {
                        "session": {"access_token": f"token-v{refresh_count[0]}"},
                        "credential_update": {
                            "access_token": f"token-v{refresh_count[0]}"
                        },
                    }

            mock_resolve.return_value = FakeGmailEngine()
            mock_build_cfg.return_value = {}
            mock_load.return_value = {"access_token": "already-refreshed-token"}
            mock_lock.return_value = MagicMock(
                __enter__=MagicMock(return_value=None),
                __exit__=MagicMock(return_value=False),
            )

            result = svc.execute_binding_action(binding_id, "list_messages")

            assert result["success"] is False
            assert refresh_count[0] == 1, f"Expected 1 refresh, got {refresh_count[0]}"
            mock_save.assert_called_once()


class TestGmailAdapterSeeding:
    def test_gmail_seeds_connector_type(self, clean_db):
        from app.services import adapter_loader

        real_adapters = str(
            Path("/srv/docker-data/projects/Apps/agent-core/data/adapters").resolve()
        )
        adapter_loader.discover_and_seed_adapters(adapters_dir=real_adapters)

        from app.services import connector_service

        ct = connector_service.get_connector_type("google_gmail")
        assert ct is not None, "google_gmail connector_type not seeded"
        assert ct["backend_type"] == "http"
        actions = ct.get("supported_actions", [])
        action_names = [a["name"] if isinstance(a, dict) else a for a in actions]
        for expected in [
            "send_email",
            "list_messages",
            "get_message",
            "search_messages",
            "modify_labels",
        ]:
            assert expected in action_names, f"Missing action: {expected}"

    def test_gmail_adapter_via_loader_with_custom_dir(self, clean_db):
        from app.services import adapter_loader

        adapter_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_adapter_dir = Path(tmpdir) / "google_gmail"
            custom_adapter_dir.mkdir()
            import shutil

            shutil.copy(adapter_path, custom_adapter_dir / "adapter.json")

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            ct = connector_service.get_connector_type("google_gmail")
            assert ct is not None
            assert ct["display_name"] == "Google Gmail"
