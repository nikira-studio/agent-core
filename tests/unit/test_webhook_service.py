"""Unit tests for webhook_service."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_webhook(
    clean_db,
    name="Test Hook",
    url="https://example.com/hook",
    secret="s3cr3t",
    event_types=None,
):
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
        hex_part = sig[len("sha256=") :]
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert hex_part == expected

    def test_different_secret_produces_different_sig(self, clean_db):
        from app.services.webhook_service import _sign_payload

        body = b"test"
        assert _sign_payload("secret1", body) != _sign_payload("secret2", body)


# ---------------------------------------------------------------------------
# Event filtering / dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_enqueues_subscribed_webhook(self, clean_db):
        from app.services.webhook_service import dispatch_event, list_deliveries

        webhook = _make_webhook(clean_db, event_types=["activity_cancelled"])
        dispatch_event("activity_cancelled", {"activity_id": "abc"})
        deliveries = list_deliveries(webhook["id"])
        assert len(deliveries) == 1
        assert deliveries[0]["event_type"] == "activity_cancelled"
        assert deliveries[0]["status"] == "pending"
        assert deliveries[0]["event_id"]

    def test_dispatch_preserves_task_result(self, clean_db):
        from app.database import get_db
        from app.services.webhook_service import dispatch_event, list_deliveries

        webhook = _make_webhook(clean_db, event_types=["activity_updated"])
        dispatch_event("activity_updated", {"activity_id": "abc", "task_note": "Progress note", "task_result": "Completed the task"})
        delivery_id = list_deliveries(webhook["id"])[0]["id"]
        with get_db() as conn:
            payload = json.loads(conn.execute("SELECT payload_json FROM webhook_delivery_log WHERE id = ?", (delivery_id,)).fetchone()["payload_json"])
        assert payload["data"]["task_note"] == "Progress note"
        assert payload["data"]["task_result"] == "Completed the task"

    def test_dispatch_skips_unsubscribed_event(self, clean_db):
        _make_webhook(clean_db, event_types=["activity_cancelled"])
        from app.services.webhook_service import dispatch_event, list_deliveries

        webhook = _make_webhook(clean_db, event_types=["activity_cancelled"])
        dispatch_event("connector_executed", {"binding_id": "x"})
        assert list_deliveries(webhook["id"]) == []

    def test_dispatch_skips_disabled_webhook(self, clean_db):
        from app.services.webhook_service import update_webhook

        wh = _make_webhook(clean_db, event_types=["activity_created"])
        update_webhook(wh["id"], enabled=False)
        from app.services.webhook_service import dispatch_event, list_deliveries

        dispatch_event("activity_created", {"activity_id": "abc"})
        assert list_deliveries(wh["id"]) == []


# ---------------------------------------------------------------------------
# Delivery log tests
# ---------------------------------------------------------------------------


class TestDeliveryLog:
    def test_delivery_logged_on_success(self, clean_db):
        from app.services.webhook_service import _enqueue_delivery, _set_delivery, list_deliveries

        wh = _make_webhook(clean_db)
        _enqueue_delivery(wh["id"], "event-success", "activity_created", {"event_type": "activity_created"})
        delivery = list_deliveries(wh["id"])[0]
        _set_delivery(delivery["id"], "success", http_status=200)
        deliveries = list_deliveries(wh["id"])
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "success"
        assert deliveries[0]["http_status"] == 200

    def test_delivery_logged_on_failure(self, clean_db):
        from app.services.webhook_service import _enqueue_delivery, _set_delivery, list_deliveries

        wh = _make_webhook(clean_db)
        _enqueue_delivery(wh["id"], "event-dead", "activity_created", {})
        delivery = list_deliveries(wh["id"])[0]
        _set_delivery(delivery["id"], "dead", http_status=500, error_message="HTTP 500")
        deliveries = list_deliveries(wh["id"])
        assert deliveries[0]["status"] == "dead"
        assert deliveries[0]["error_message"] == "HTTP 500"

    def test_disabled_webhook_cancels_queued_deliveries(self, clean_db):
        from app.services.webhook_service import dispatch_event, list_deliveries, update_webhook

        webhook = _make_webhook(clean_db, event_types=["activity_created"])
        dispatch_event("activity_created", {"activity_id": "queued"})
        update_webhook(webhook["id"], enabled=False)
        assert list_deliveries(webhook["id"])[0]["status"] == "cancelled"

    def test_deleted_webhook_retains_cancelled_delivery_history(self, clean_db):
        from app.services.webhook_service import delete_webhook, dispatch_event, list_deliveries

        webhook = _make_webhook(clean_db, event_types=["activity_created"])
        dispatch_event("activity_created", {"activity_id": "queued"})
        assert delete_webhook(webhook["id"])
        assert list_deliveries(webhook["id"])[0]["status"] == "cancelled"

    def test_failed_delivery_waits_for_retry_with_stable_event_id(self, clean_db):
        from app.services.webhook_service import dispatch_event, list_deliveries, run_delivery_cycle

        webhook = _make_webhook(clean_db, event_types=["activity_created"])
        dispatch_event("activity_created", {"activity_id": "retry"})
        before = list_deliveries(webhook["id"])[0]

        response = MagicMock()
        response.status_code = 500
        with patch("app.services.webhook_service.safe_httpx_post", return_value=response) as post:
            assert run_delivery_cycle() is True

        after = list_deliveries(webhook["id"])[0]
        assert after["status"] == "retry_wait"
        assert after["attempt_count"] == 1
        assert after["event_id"] == before["event_id"]
        assert post.call_args.kwargs["headers"]["X-Agent-Core-Event-Id"] == before["event_id"]

    def test_client_error_becomes_dead_without_retry(self, clean_db):
        from app.services.webhook_service import dispatch_event, list_deliveries, run_delivery_cycle

        webhook = _make_webhook(clean_db, event_types=["activity_created"])
        dispatch_event("activity_created", {"activity_id": "bad-request"})
        response = MagicMock(status_code=400, headers={})
        with patch("app.services.webhook_service.safe_httpx_post", return_value=response):
            assert run_delivery_cycle() is True
        delivery = list_deliveries(webhook["id"])[0]
        assert delivery["status"] == "dead"
        assert delivery["attempt_count"] == 1

    def test_rate_limit_uses_bounded_retry_after(self, clean_db):
        from app.services.webhook_service import dispatch_event, list_deliveries, run_delivery_cycle

        webhook = _make_webhook(clean_db, event_types=["activity_created"])
        dispatch_event("activity_created", {"activity_id": "rate-limited"})
        response = MagicMock(status_code=429, headers={"retry-after": "99999"})
        with patch("app.services.webhook_service.safe_httpx_post", return_value=response), patch(
            "app.services.webhook_service.webhook_settings_service.retry_policy",
            return_value={"webhook_retry_max_attempts": 5, "webhook_retry_initial_seconds": 1, "webhook_retry_max_seconds": 10, "webhook_retry_jitter_seconds": 0},
        ):
            assert run_delivery_cycle() is True
        delivery = list_deliveries(webhook["id"])[0]
        assert delivery["status"] == "retry_wait"
        assert 0 <= (datetime.fromisoformat(delivery["next_attempt_at"]) - datetime.now(timezone.utc)).total_seconds() <= 10


# ---------------------------------------------------------------------------
# Test delivery tests
# ---------------------------------------------------------------------------


class TestTestDelivery:
    def test_test_delivery_uses_synthetic_payload(self, clean_db):
        wh = _make_webhook(clean_db)
        posted_bodies = []

        def mock_post(_client, url, *, content, headers):
            posted_bodies.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch(
            "app.services.webhook_service.safe_httpx_post", side_effect=mock_post
        ):
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

        def mock_post(_client, url, *, content, headers):
            posted_bodies.append(json.loads(content))
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch(
            "app.services.webhook_service.safe_httpx_post", side_effect=mock_post
        ):
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
        from app.services.webhook_service import _enqueue_delivery, test_delivery

        wh = _make_webhook(clean_db)
        _enqueue_delivery(wh["id"], "event-prior", "activity_created", {"real": "payload"})

        def mock_post(_client, url, *, content, headers):
            payload = json.loads(content)
            assert payload["event_type"] == "test", (
                "test delivery must use synthetic payload, not replay"
            )
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch(
            "app.services.webhook_service.safe_httpx_post", side_effect=mock_post
        ):
            result = test_delivery(wh["id"])
        assert result["ok"] is True
