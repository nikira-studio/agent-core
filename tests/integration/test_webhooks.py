"""Integration tests for webhook registration and delivery."""
import json
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


@pytest.fixture
def user_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("user1", "user1@test.local", "testpassword123", "User One", "user")
    session = create_session("user1", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


def _create_webhook(client, name="Test Hook", url="https://example.com/hook", secret="s3cr3t", event_types=None):
    with patch("app.security.url_validation.validate_public_url"):
        return client.post("/api/webhooks", json={
            "name": name,
            "url": url,
            "secret": secret,
            "event_types": event_types or ["activity_created", "activity_cancelled"],
        })


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------

class TestWebhookAdminCRUD:
    def test_admin_can_create_webhook(self, admin_client):
        r = _create_webhook(admin_client)
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["webhook"]["name"] == "Test Hook"
        assert "activity_created" in data["webhook"]["event_types"]

    def test_admin_can_list_webhooks(self, admin_client):
        _create_webhook(admin_client, name="Hook A")
        _create_webhook(admin_client, name="Hook B")
        r = admin_client.get("/api/webhooks")
        assert r.status_code == 200
        names = [w["name"] for w in r.json()["data"]["webhooks"]]
        assert "Hook A" in names
        assert "Hook B" in names

    def test_admin_can_update_webhook(self, admin_client):
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]
        with patch("app.security.url_validation.validate_public_url"):
            r = admin_client.put(f"/api/webhooks/{wh_id}", json={"name": "Renamed", "enabled": False})
        assert r.status_code == 200
        updated = r.json()["data"]["webhook"]
        assert updated["name"] == "Renamed"
        assert updated["enabled"] is False

    def test_admin_can_delete_webhook(self, admin_client):
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]
        r = admin_client.delete(f"/api/webhooks/{wh_id}")
        assert r.status_code == 200
        r2 = admin_client.get(f"/api/webhooks/{wh_id}")
        assert r2.status_code == 404

    def test_non_admin_cannot_create_webhook(self, user_client):
        with patch("app.security.url_validation.validate_public_url"):
            r = user_client.post("/api/webhooks", json={
                "name": "x", "url": "https://example.com", "secret": "x", "event_types": ["activity_created"]
            })
        assert r.status_code == 403

    def test_non_admin_cannot_list_webhooks(self, user_client):
        r = user_client.get("/api/webhooks")
        assert r.status_code == 403

    def test_secret_not_returned_in_response(self, admin_client):
        r = _create_webhook(admin_client)
        data = r.json()["data"]["webhook"]
        assert "secret" not in data
        assert "secret_encrypted" not in data

    def test_invalid_event_type_rejected(self, admin_client):
        with patch("app.security.url_validation.validate_public_url"):
            r = admin_client.post("/api/webhooks", json={
                "name": "x", "url": "https://example.com", "secret": "x",
                "event_types": ["not_a_real_event"],
            })
        assert r.status_code == 400

    def test_empty_event_types_rejected(self, admin_client):
        with patch("app.security.url_validation.validate_public_url"):
            r = admin_client.post("/api/webhooks", json={
                "name": "x", "url": "https://example.com", "secret": "x",
                "event_types": [],
            })
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Delivery tests
# ---------------------------------------------------------------------------

class TestWebhookDelivery:
    def test_delivery_logged_on_subscribed_event(self, admin_client):
        from app.services import webhook_service
        wh_id = _create_webhook(admin_client, event_types=["activity_created"]).json()["data"]["webhook"]["id"]

        posted = []
        def mock_post(url, *, content, headers, timeout):
            posted.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            webhook_service.dispatch_event(
                "activity_created",
                {
                    "activity_id": "abc",
                    "task_description": "Test task",
                    "agent_id": "a1",
                    "assigned_agent_id": "a1",
                    "memory_scope": "agent:a1",
                    "status": "active",
                },
            )
            import time
            time.sleep(0.1)

        assert len(posted) == 1
        assert posted[0]["event_type"] == "activity_created"
        assert posted[0]["data"]["activity_id"] == "abc"
        assert posted[0]["data"]["task_description"] == "Test task"

        r = admin_client.get(f"/api/webhooks/{wh_id}/deliveries")
        deliveries = r.json()["data"]["deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "success"
        assert deliveries[0]["http_status"] == 200

    def test_unsubscribed_event_does_not_trigger_delivery(self, admin_client):
        from app.services import webhook_service
        _create_webhook(admin_client, event_types=["activity_cancelled"])
        wh_id = admin_client.get("/api/webhooks").json()["data"]["webhooks"][0]["id"]

        with patch("threading.Thread") as mock_thread:
            webhook_service.dispatch_event("activity_created", {"activity_id": "abc"})
            mock_thread.assert_not_called()

        r = admin_client.get(f"/api/webhooks/{wh_id}/deliveries")
        assert r.json()["data"]["total"] == 0

    def test_failed_delivery_is_recorded(self, admin_client):
        from app.services import webhook_service
        wh_id = _create_webhook(admin_client, event_types=["activity_created"]).json()["data"]["webhook"]["id"]

        def mock_post(url, *, content, headers, timeout):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        with patch("httpx.post", side_effect=mock_post):
            webhook_service.dispatch_event("activity_created", {"activity_id": "abc"})
            import time
            time.sleep(0.1)

        r = admin_client.get(f"/api/webhooks/{wh_id}/deliveries")
        deliveries = r.json()["data"]["deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "failure"

    def test_signed_payload_has_correct_header(self, admin_client):
        from app.services import webhook_service
        _create_webhook(admin_client, event_types=["activity_created"], secret="mywebhooksecret")

        sent_headers = []
        def mock_post(url, *, content, headers, timeout):
            sent_headers.append(dict(headers))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            webhook_service.dispatch_event("activity_created", {"activity_id": "test"})
            import time
            time.sleep(0.1)

        assert len(sent_headers) == 1
        sig = sent_headers[0].get("X-Agent-Core-Signature", "")
        assert sig.startswith("sha256=")

    def test_connector_event_payload_includes_binding_context(self, admin_client):
        from app.services import connector_service, webhook_service

        webhook_service.create_webhook(
            name="Connector Hook",
            url="https://example.com/hook",
            secret_plaintext="secret",
            event_types=["connector_executed"],
            created_by="admin",
        )
        ct = connector_service.create_connector_type(
            connector_type_id="hook-test-connector",
            display_name="Hook Test Connector",
            provider_type="builtin",
            auth_type="none",
            supported_actions=["ping"],
        )
        binding = connector_service.create_binding(
            connector_type_id=ct["id"],
            name="Hook Binding",
            scope="user:admin",
            enabled=True,
            created_by="admin",
        )

        captured = []

        def mock_post(url, *, content, headers, timeout):
            captured.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            webhook_service.dispatch_event(
                "connector_executed",
                {
                    "binding_id": binding["id"],
                    "binding_name": binding["name"],
                    "scope": binding["scope"],
                    "connector_type_id": ct["id"],
                    "connector_type_name": ct["display_name"],
                    "action": "ping",
                    "success": True,
                    "duration_ms": 12,
                    "status": "success",
                },
            )
            import time
            time.sleep(0.1)

        assert captured
        payload = captured[0]["data"]
        assert payload["binding_id"] == binding["id"]
        assert payload["binding_name"] == "Hook Binding"
        assert payload["connector_type_id"] == ct["id"]
        assert payload["connector_type_name"] == "Hook Test Connector"
        assert payload["action"] == "ping"
        assert payload["success"] is True

    def test_disabled_webhook_not_triggered(self, admin_client):
        from app.services import webhook_service
        wh_id = _create_webhook(admin_client, event_types=["activity_created"]).json()["data"]["webhook"]["id"]
        with patch("app.security.url_validation.validate_public_url"):
            admin_client.put(f"/api/webhooks/{wh_id}", json={"enabled": False})

        with patch("threading.Thread") as mock_thread:
            webhook_service.dispatch_event("activity_created", {"activity_id": "abc"})
            mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Test delivery endpoint
# ---------------------------------------------------------------------------

class TestTestDelivery:
    def test_test_delivery_sends_synthetic_payload(self, admin_client):
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]
        posted = []

        def mock_post(url, *, content, headers, timeout):
            posted.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            r = admin_client.post(f"/api/webhooks/{wh_id}/test")

        assert r.status_code == 200
        assert r.json()["data"]["ok"] is True
        assert len(posted) == 1
        assert posted[0]["event_type"] == "test"
        assert "message" in posted[0]["data"]

    def test_test_delivery_does_not_replay_real_delivery(self, admin_client):
        from app.services import webhook_service
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]

        # simulate a prior real delivery in the log
        webhook_service._record_delivery(wh_id, "activity_created", '{"real":"data"}', "success", 200, None)

        def mock_post(url, *, content, headers, timeout):
            payload = json.loads(content)
            assert payload["event_type"] == "test", "should use synthetic payload, not replay prior"
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            r = admin_client.post(f"/api/webhooks/{wh_id}/test")
        assert r.json()["data"]["ok"] is True

    def test_test_delivery_logged(self, admin_client):
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]

        def mock_post(url, *, content, headers, timeout):
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            admin_client.post(f"/api/webhooks/{wh_id}/test")

        r = admin_client.get(f"/api/webhooks/{wh_id}/deliveries")
        deliveries = r.json()["data"]["deliveries"]
        assert any(d["event_type"] == "test" for d in deliveries)

    def test_test_delivery_failure_logged(self, admin_client):
        wh_id = _create_webhook(admin_client).json()["data"]["webhook"]["id"]

        def mock_post(url, *, content, headers, timeout):
            resp = MagicMock()
            resp.status_code = 503
            return resp

        with patch("httpx.post", side_effect=mock_post):
            r = admin_client.post(f"/api/webhooks/{wh_id}/test")

        data = r.json()["data"]
        assert data["http_status"] == 503
        deliveries = admin_client.get(f"/api/webhooks/{wh_id}/deliveries").json()["data"]["deliveries"]
        assert any(d["status"] == "failure" for d in deliveries)


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------

class TestWebhooksDashboardPage:
    def test_webhooks_page_loads_for_admin(self, admin_client):
        r = admin_client.get("/webhooks")
        assert r.status_code == 200
        assert "Webhooks" in r.text

    def test_webhooks_page_forbidden_for_non_admin(self, user_client):
        r = user_client.get("/webhooks")
        assert r.status_code == 403
        assert "Admin Access Required" in r.text

    def test_webhooks_page_has_create_modal(self, admin_client):
        r = admin_client.get("/webhooks")
        assert 'id="create-webhook-modal"' in r.text
        assert "createWebhook" in r.text

    def test_webhooks_page_has_edit_modal(self, admin_client):
        r = admin_client.get("/webhooks")
        assert 'id="edit-webhook-modal"' in r.text
        assert "submitEditWebhook" in r.text

    def test_webhooks_page_has_deliveries_modal(self, admin_client):
        r = admin_client.get("/webhooks")
        assert 'id="deliveries-modal"' in r.text
        assert "viewDeliveries" in r.text

    def test_webhooks_nav_hidden_for_non_admin(self, user_client):
        r = user_client.get("/")
        assert r.status_code == 200
        assert 'href="/webhooks"' not in r.text

    def test_webhooks_page_shows_registered_webhooks(self, admin_client):
        from app.services import webhook_service
        webhook_service.create_webhook(
            name="My Integration",
            url="https://n8n.example.com/webhook/agent-core",
            secret_plaintext="mysecret",
            event_types=["activity_cancelled"],
            created_by="admin",
        )
        r = admin_client.get("/webhooks")
        assert "My Integration" in r.text
        assert "activity_cancelled" in r.text
