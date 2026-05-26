"""Integration tests for the Google Gmail adapter manifest."""

import base64
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate


def make_gmail_ct(backend_json: dict) -> dict:
    return {"id": "google_gmail", "backend_json": json.dumps(backend_json)}


def make_gmail_cred(fields: dict | None = None) -> Credential:
    return Credential(
        raw=None,
        fields=fields
        or {"client_id": "cid", "client_secret": "csec", "refresh_token": "rt"},
        reference_name="gmail-cref",
    )


class TestGmailAdapterWireLevel:
    def test_send_email_renders_valid_rfc822_base64_raw(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(return_value=json.dumps({"id": "msg123"}).encode()),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "send_email",
            {"to": ["alice@example.com"], "subject": "Hello", "body": "World"},
            make_gmail_cred(),
            None,
            session=None,
        )

        assert result["success"] is True
        assert len(captured) == 1
        call = captured[0]

        assert "gmail.googleapis.com" in call["url"]
        assert "/gmail/v1/messages/send" in call["url"]

        body = json.loads(json.dumps(call["body"]))
        assert "raw" in body
        raw = body["raw"]
        assert raw, "raw field must not be empty"

        padded = raw + "=" * (4 - len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        assert "Subject: Hello" in decoded, f"RFC822 should contain subject: {decoded}"
        assert "World" in decoded, f"RFC822 should contain body: {decoded}"
        assert "To: alice@example.com" in decoded

    def test_send_email_with_cc_and_bcc(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(return_value=json.dumps({"id": "msg456"}).encode()),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "send_email",
            {
                "to": ["alice@example.com", "bob@example.com"],
                "subject": "Meeting",
                "body": "Let's meet tomorrow.",
                "cc": ["carol@example.com"],
                "bcc": ["david@example.com"],
            },
            make_gmail_cred(),
            None,
            session=None,
        )

        assert result["success"] is True
        body = json.loads(json.dumps(captured[0]["body"]))
        padded = body["raw"] + "=" * (4 - len(body["raw"]) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        assert "To: alice@example.com, bob@example.com" in decoded
        assert "Cc: carol@example.com" in decoded
        assert "Bcc: david@example.com" in decoded

    def test_list_messages_renders_correct_query_params(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps(
                        {"messages": [{"id": "msg1"}, {"id": "msg2"}]}
                    ).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "list_messages",
            {"label": "SENT", "max_results": 25},
            make_gmail_cred(),
            None,
            session={"access_token": "test-token"},
        )

        assert result["success"] is True
        assert len(captured) == 1
        call = captured[0]

        assert "gmail.googleapis.com" in call["url"]
        assert "/gmail/v1/users/me/messages" in call["url"]
        assert "labelIds=SENT" in call["url"]
        assert "maxResults=25" in call["url"]
        assert "Authorization" in call["headers"]
        assert "Bearer test-token" in call["headers"]["Authorization"]

    def test_list_messages_defaults_label_and_max_results(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(return_value=json.dumps({"messages": []}).encode()),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "list_messages", {}, make_gmail_cred(), None, session=None
        )

        assert result["success"] is True
        call = captured[0]
        assert "labelIds=INBOX" in call["url"]
        assert "maxResults=10" in call["url"]

    def test_get_message_renders_correct_path(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps({"id": "abc", "snippet": "hi"}).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "get_message",
            {"message_id": "msg_abc123"},
            make_gmail_cred(),
            None,
            session=None,
        )

        assert result["success"] is True
        call = captured[0]
        assert "/gmail/v1/users/me/messages/msg_abc123" in call["url"]
        assert "format=full" in call["url"]

    def test_modify_labels_renders_correct_body(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = HttpEngine(
            {"id": "google_gmail", "backend_json": json.dumps(m.backend)}
        )

        captured = []
        engine._send = MagicMock(
            side_effect=lambda req, cfg: captured.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps(
                        {"id": "msg789", "labelIds": ["STARRED"]}
                    ).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "modify_labels",
            {
                "message_id": "msg_xyz",
                "add_labels": ["STARRED"],
                "remove_labels": ["INBOX"],
            },
            make_gmail_cred(),
            None,
            session=None,
        )

        assert result["success"] is True
        body = json.loads(json.dumps(captured[0]["body"]))
        assert body.get("addLabelIds") == ["STARRED"]
        assert body.get("removeLabelIds") == ["INBOX"]


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
