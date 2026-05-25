"""Unit tests for webhook_service."""
import hashlib
import hmac
import json
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_webhook(clean_db, name="Test Hook", url="https://example.com/hook", secret="s3cr3t", event_types=None):
    from app.services.webhook_service import create_webhook
    return create_webhook(
        name=name,
        url=url,
        secret_plaintext=secret,
        event_types=event_types or ["activity_created", "activity_cancelled"],
        created_by="admin",
    )


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

class TestWebhookCRUD:
    def test_create_and_get(self, clean_db):
        from app.services.webhook_service import get_webhook
        wh = _make_webhook(clean_db)
        assert wh["id"]
        assert wh["name"] == "Test Hook"
        assert wh["url"] == "https://example.com/hook"
        assert wh["enabled"] is True
        assert "activity_created" in wh["event_types"]
        fetched = get_webhook(wh["id"])
        assert fetched["id"] == wh["id"]

    def test_list_webhooks(self, clean_db):
        from app.services.webhook_service import list_webhooks
        _make_webhook(clean_db, name="Hook A")
        _make_webhook(clean_db, name="Hook B")
        hooks = list_webhooks()
        names = [h["name"] for h in hooks]
        assert "Hook A" in names
        assert "Hook B" in names

    def test_update_webhook(self, clean_db):
        from app.services.webhook_service import update_webhook, get_webhook
        wh = _make_webhook(clean_db)
        updated = update_webhook(wh["id"], name="Renamed", enabled=False)
        assert updated is True
        fetched = get_webhook(wh["id"])
        assert fetched["name"] == "Renamed"
        assert fetched["enabled"] is False

    def test_delete_webhook(self, clean_db):
        from app.services.webhook_service import delete_webhook, get_webhook
        wh = _make_webhook(clean_db)
        assert delete_webhook(wh["id"]) is True
        assert get_webhook(wh["id"]) is None

    def test_secret_not_in_list_response(self, clean_db):
        from app.services.webhook_service import list_webhooks
        _make_webhook(clean_db)
        hooks = list_webhooks()
        for h in hooks:
            assert "secret" not in h
            assert "secret_encrypted" not in h

    def test_unknown_event_types_filtered_out(self, clean_db):
        from app.services.webhook_service import create_webhook, get_webhook
        wh = create_webhook(
            name="Filtered",
            url="https://example.com/hook",
            secret_plaintext="s3cr3t",
            event_types=["activity_created", "not_a_real_event"],
            created_by="admin",
        )
        fetched = get_webhook(wh["id"])
        assert "not_a_real_event" not in fetched["event_types"]
        assert "activity_created" in fetched["event_types"]


# ---------------------------------------------------------------------------
# Signing tests
# ---------------------------------------------------------------------------

class TestSigning:
    def test_sign_payload_produces_sha256_prefix(self, clean_db):
        from app.services.webhook_service import _sign_payload
        sig = _sign_payload("mysecret", b'{"hello":"world"}')
        assert sig.startswith("sha256=")

    def test_sign_payload_verifiable(self, clean_db):
        from app.services.webhook_service import _sign_payload
        body = b'{"event_type":"activity_created"}'
        secret = "mysecret"
        sig = _sign_payload(secret, body)
        hex_part = sig[len("sha256="):]
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert hex_part == expected

    def test_different_secret_produces_different_sig(self, clean_db):
        from app.services.webhook_service import _sign_payload
        body = b'test'
        assert _sign_payload("secret1", body) != _sign_payload("secret2", body)


# ---------------------------------------------------------------------------
# Event filtering / dispatch tests
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_dispatch_calls_subscribed_webhook(self, clean_db):
        _make_webhook(clean_db, event_types=["activity_cancelled"])
        with patch("app.services.webhook_service._deliver_one") as mock_deliver:
            from app.services.webhook_service import dispatch_event
            dispatch_event("activity_cancelled", {"activity_id": "abc"})
            import time
            time.sleep(0.05)
            mock_deliver.assert_called_once()
            args = mock_deliver.call_args[0]
            assert args[3] == "activity_cancelled"

    def test_dispatch_preserves_task_result(self, clean_db):
        _make_webhook(clean_db, event_types=["activity_updated"])
        captured = []

        def fake_deliver(webhook_id, url, secret_encrypted, event_type, payload):
            captured.append(payload)

        with patch("app.services.webhook_service._deliver_one", side_effect=fake_deliver):
            from app.services.webhook_service import dispatch_event
            dispatch_event(
                "activity_updated",
                {
                    "activity_id": "abc",
                    "task_note": "Progress note",
                    "task_result": "Completed the task",
                },
            )
            import time
            time.sleep(0.05)

        assert captured
        assert captured[0]["data"]["task_note"] == "Progress note"
        assert captured[0]["data"]["task_result"] == "Completed the task"

    def test_dispatch_skips_unsubscribed_event(self, clean_db):
        _make_webhook(clean_db, event_types=["activity_cancelled"])
        with patch("threading.Thread") as mock_thread:
            from app.services.webhook_service import dispatch_event
            dispatch_event("connector_executed", {"binding_id": "x"})
            mock_thread.assert_not_called()

    def test_dispatch_skips_disabled_webhook(self, clean_db):
        from app.services.webhook_service import update_webhook
        wh = _make_webhook(clean_db, event_types=["activity_created"])
        update_webhook(wh["id"], enabled=False)
        with patch("threading.Thread") as mock_thread:
            from app.services.webhook_service import dispatch_event
            dispatch_event("activity_created", {"activity_id": "abc"})
            mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Delivery log tests
# ---------------------------------------------------------------------------

class TestDeliveryLog:
    def test_delivery_logged_on_success(self, clean_db):
        from app.services.webhook_service import list_deliveries, _record_delivery
        wh = _make_webhook(clean_db)
        _record_delivery(wh["id"], "activity_created", '{"event_type":"activity_created"}', "success", 200, None)
        deliveries = list_deliveries(wh["id"])
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "success"
        assert deliveries[0]["http_status"] == 200

    def test_delivery_logged_on_failure(self, clean_db):
        from app.services.webhook_service import list_deliveries, _record_delivery
        wh = _make_webhook(clean_db)
        _record_delivery(wh["id"], "activity_created", '{}', "failure", 500, "HTTP 500")
        deliveries = list_deliveries(wh["id"])
        assert deliveries[0]["status"] == "failure"
        assert deliveries[0]["error_message"] == "HTTP 500"


# ---------------------------------------------------------------------------
# Test delivery tests
# ---------------------------------------------------------------------------

class TestTestDelivery:
    def test_test_delivery_uses_synthetic_payload(self, clean_db):
        wh = _make_webhook(clean_db)
        posted_bodies = []

        def mock_post(url, *, content, headers, timeout):
            posted_bodies.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            from app.services.webhook_service import test_delivery
            result = test_delivery(wh["id"])

        assert result["ok"] is True
        assert len(posted_bodies) == 1
        payload = posted_bodies[0]
        assert payload["event_type"] == "test"
        assert "timestamp" in payload
        assert "data" in payload
        from app.branding import APP_NAME
        assert payload["data"]["message"] == f"{APP_NAME} webhook test delivery"

    def test_activity_updated_test_delivery_includes_task_note(self, clean_db):
        wh = _make_webhook(clean_db)
        posted_bodies = []

        def mock_post(url, *, content, headers, timeout):
            posted_bodies.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            from app.services.webhook_service import test_delivery
            result = test_delivery(wh["id"], event_type="activity_updated")

        assert result["ok"] is True
        assert len(posted_bodies) == 1
        payload = posted_bodies[0]
        assert payload["event_type"] == "activity_updated"
        assert payload["data"]["status"] == "active"
        assert payload["data"]["task_note"] == "Applied a sample progress update"
        assert payload["data"]["task_result"] is None

    def test_test_delivery_does_not_replay_prior_delivery(self, clean_db):
        from app.services.webhook_service import _record_delivery, test_delivery
        wh = _make_webhook(clean_db)
        _record_delivery(wh["id"], "activity_created", '{"real":"payload"}', "success", 200, None)

        def mock_post(url, *, content, headers, timeout):
            payload = json.loads(content)
            assert payload["event_type"] == "test", "test delivery must use synthetic payload, not replay"
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=mock_post):
            result = test_delivery(wh["id"])
        assert result["ok"] is True
