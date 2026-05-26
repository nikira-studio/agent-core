"""Integration tests for the connector session refresh loop."""

import json
from unittest.mock import MagicMock, patch


import app.services.credential_service as cred_svc


class TestConnectorSessionRefresh:
    def test_execute_binding_action_refreshes_session_on_409(self, monkeypatch):
        """Http adapter that 409s twice, then succeeds. refresh_session called once."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "test-session-binding"

        with (
            patch.object(svc, "get_binding") as mock_get_binding,
            patch.object(svc, "get_connector_type") as mock_get_ct,
            patch.object(svc, "_validate_action_for_connector") as mock_validate,
            patch.object(svc, "_check_rate_limit") as mock_rate_limit,
            patch.object(svc, "get_binding_with_credential") as _mock_get_bwc,
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
                "connector_type_id": "http_conn",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": '{"base_url": "http://localhost:9091"}',
            }
            mock_get_ct.return_value = {
                "id": "http_conn",
                "backend_type": "http",
                "auth_type": "http",
                "backend_json": json.dumps(
                    {"requests": {"ping": {"method": "GET", "path": "/ping"}}}
                ),
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None

            refresh_call_count = [0]
            execute_call_count = [0]

            class FakeHttpEngine:
                needs_session = True

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    execute_call_count[0] += 1
                    if execute_call_count[0] <= 2:
                        from app.connectors.errors import SessionExpiredError

                        raise SessionExpiredError()
                    return {"success": True, "body": "pong"}

                def refresh_session(self, credential, config_json, current_session):
                    refresh_call_count[0] += 1
                    return {
                        "session": {"session_id": "refreshed-abc"},
                        "expires_at": None,
                    }

            mock_resolve.return_value = FakeHttpEngine()
            mock_build_cfg.return_value = {}
            mock_load.return_value = None
            mock_lock.return_value = MagicMock(
                __enter__=MagicMock(), __exit__=MagicMock()
            )

            with patch.object(
                cred_svc,
                "resolve_credential",
                return_value=MagicMock(
                    raw=None,
                    fields={"username": "a", "password": "b"},
                    reference_name="test-ref",
                ),
            ):
                result = svc.execute_binding_action(binding_id, "ping")

            assert result["success"] is True
            assert execute_call_count[0] == 3
            assert refresh_call_count[0] == 1
            mock_save.assert_called_once_with(
                binding_id, {"session_id": "refreshed-abc"}, None
            )

    def test_execute_binding_action_caches_session_after_success(self, monkeypatch):
        """After successful execution, session is cached."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "test-session-cache"

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
            patch.object(svc.time, "sleep") as _mock_sleep,
        ):
            mock_get_binding.return_value = {
                "id": binding_id,
                "enabled": True,
                "connector_type_id": "http_conn",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": '{"base_url": "http://localhost:9091"}',
            }
            mock_get_ct.return_value = {
                "id": "http_conn",
                "backend_type": "http",
                "backend_json": json.dumps({}),
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None
            mock_get_bwc.return_value = {
                "credential": MagicMock(
                    raw=None,
                    fields={"username": "a", "password": "b"},
                    reference_name="test-ref",
                ),
                "credential_plaintext": None,
            }

            class FakeHttpEngine:
                needs_session = True

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    return {"success": True, "session_used": session}

                def refresh_session(self, credential, config_json, current_session):
                    return {"session": {"session_id": "fresh"}, "expires_at": None}

            mock_resolve.return_value = FakeHttpEngine()
            mock_build_cfg.return_value = {}
            mock_load.return_value = {"session_id": "cached-xyz"}

            result = svc.execute_binding_action(binding_id, "ping")

            assert result["success"] is True
            assert result.get("session_used", {}).get("session_id") == "cached-xyz"

    def test_execute_binding_action_stateless_skips_session(self, monkeypatch):
        """A stateless connector (needs_session=False) never touches session cache."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "stateless-binding"

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
                "connector_type_id": "stateless_conn",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": None,
            }
            mock_get_ct.return_value = {
                "id": "stateless_conn",
                "backend_type": "openapi",
                "auth_type": "none",
                "operations_json": None,
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None
            mock_get_bwc.return_value = {
                "credential": None,
                "credential_plaintext": None,
            }

            execute_call_count = [0]

            class FakeStatelessExecutor:
                needs_session = False

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    execute_call_count[0] += 1
                    assert session is None, (
                        "Stateless connector should receive session=None"
                    )
                    return {"success": True}

            mock_resolve.return_value = FakeStatelessExecutor()
            mock_build_cfg.return_value = {}

            result = svc.execute_binding_action(binding_id, "call_endpoint")

            assert result["success"] is True
            assert execute_call_count[0] == 1
            mock_load.assert_not_called()
            mock_save.assert_not_called()
            mock_lock.assert_not_called()


class TestConnectorOAuthRotation:
    def test_oauth_rotation_updates_credential(self, monkeypatch):
        """Rotated access token from refresh_session is persisted via update_credential_value."""
        import app.services.connector_service as svc
        import app.services.connector_session_service as sessions

        binding_id = "oauth-binding"

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
            patch.object(cred_svc, "update_credential_value") as mock_update_cred,
            patch.object(cred_svc, "resolve_credential") as mock_resolve_cred,
        ):
            mock_get_binding.return_value = {
                "id": binding_id,
                "enabled": True,
                "connector_type_id": "gmail",
                "rate_limit_config_json": '{"max_retries": 0}',
                "config_json": None,
            }
            mock_get_ct.return_value = {
                "id": "gmail",
                "backend_type": "http",
                "auth_type": "http",
                "backend_json": json.dumps({}),
            }
            mock_validate.return_value = None
            mock_rate_limit.return_value = None

            cred_obj = MagicMock(
                raw=None,
                fields={"refresh_token": "old_refresh"},
                reference_name="gmail-cref",
            )
            mock_get_bwc.return_value = {
                "credential": cred_obj,
                "credential_plaintext": None,
            }

            updated_cred_obj = MagicMock(
                raw=None,
                fields={"refresh_token": "old_refresh", "access_token": "new-token"},
                reference_name="gmail-cref",
            )
            mock_resolve_cred.return_value = updated_cred_obj

            execute_count = [0]

            class FakeOAuthEngine:
                needs_session = True

                def execute(
                    self, action, params, credential, config_json, session=None
                ):
                    execute_count[0] += 1
                    from app.connectors.errors import AuthExpiredError

                    # Calls 1 & 2 raise (initial + double-check after lock);
                    # call 3 succeeds after refresh_session has rotated the token.
                    if execute_count[0] < 3:
                        raise AuthExpiredError()
                    return {"success": True, "body": {"sent": True}}

                def refresh_session(self, credential, config_json, current_session):
                    return {
                        "session": {
                            "access_token": "new-token",
                            "expires_at": "9999999999",
                        },
                        "credential_update": {"access_token": "new-token"},
                    }

            mock_resolve.return_value = FakeOAuthEngine()
            mock_build_cfg.return_value = {}
            mock_load.return_value = None
            # __exit__ must return falsy so exceptions raised inside the `with` are
            # NOT suppressed. A bare MagicMock() returns a MagicMock (truthy) when
            # called, which silently swallows exceptions and makes _run_once return None.
            mock_lock.return_value = MagicMock(
                __enter__=MagicMock(return_value=None),
                __exit__=MagicMock(return_value=False),
            )

            result = svc.execute_binding_action(binding_id, "send_email")

            assert result["success"] is True, f"expected success, got {result!r}"
            assert execute_count[0] == 3
            mock_update_cred.assert_called_once()
            call_args = mock_update_cred.call_args
            assert call_args[0][0] == "gmail-cref"
            persisted = json.loads(call_args[0][1])
            assert persisted["access_token"] == "new-token"
