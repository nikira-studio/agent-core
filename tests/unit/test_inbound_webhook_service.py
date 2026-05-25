"""Unit tests for inbound_webhook_service."""
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def clean_db(clean_db):
    return clean_db


class TestKeyManagement:
    def test_no_key_by_default(self):
        from app.services import inbound_webhook_service as svc
        assert not svc.has_active_key()
        assert svc.get_active_key_row() is None

    def test_generate_key_returns_prefixed_plaintext(self):
        from app.services import inbound_webhook_service as svc
        key = svc.generate_key()
        assert key.startswith("ac_inbound_")

    def test_generate_key_activates_key(self):
        from app.services import inbound_webhook_service as svc
        svc.generate_key()
        assert svc.has_active_key()

    def test_generate_key_twice_raises(self):
        from app.services import inbound_webhook_service as svc
        svc.generate_key()
        with pytest.raises(ValueError, match="already exists"):
            svc.generate_key()

    def test_verify_key_correct(self):
        from app.services import inbound_webhook_service as svc
        plaintext = svc.generate_key()
        assert svc.verify_key(plaintext) is True

    def test_verify_key_wrong(self):
        from app.services import inbound_webhook_service as svc
        svc.generate_key()
        assert svc.verify_key("not-the-right-key") is False

    def test_verify_key_no_key_exists(self):
        from app.services import inbound_webhook_service as svc
        assert svc.verify_key("ac_inbound_anything") is False

    def test_rotate_key_invalidates_previous(self):
        from app.services import inbound_webhook_service as svc
        old = svc.generate_key()
        svc.rotate_key()
        assert svc.verify_key(old) is False

    def test_rotate_key_issues_new_valid_key(self):
        from app.services import inbound_webhook_service as svc
        svc.generate_key()
        new_key = svc.rotate_key()
        assert svc.verify_key(new_key) is True

    def test_rotate_without_existing_key_raises(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="No active key"):
            svc.rotate_key()

    def test_get_active_key_row_after_rotate(self):
        from app.services import inbound_webhook_service as svc
        svc.generate_key()
        svc.rotate_key()
        row = svc.get_active_key_row()
        assert row is not None
        assert row["rotated_at"] is None  # new row has no rotated_at yet


class TestEventTypeValidation:
    def test_unknown_event_type_raises(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="Unknown event type"):
            svc.handle_inbound("activity.delete", {})

    def test_modify_events_require_activity_id(self):
        from app.services import inbound_webhook_service as svc
        for event_type in ("activity.assign", "activity.update", "activity.cancel", "activity.note"):
            with pytest.raises(ValueError, match="activity_id"):
                svc.handle_inbound(event_type, {})


class TestActivityCreate:
    def test_create_requires_assigned_agent_id(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="assigned_agent_id"):
            svc.handle_inbound("activity.create", {"task_description": "do something"})

    def test_create_returns_activity_id(self):
        from app.services import inbound_webhook_service as svc
        mock_activity = {
            "id": "act-123",
            "status": "active",
            "agent_id": "codex",
            "assigned_agent_id": "codex",
            "task_description": "review code",
            "user_id": "inbound-webhook",
            "memory_scope": None,
        }
        with patch("app.services.inbound_webhook_service.activity_service.create_activity", return_value=mock_activity):
            result = svc.handle_inbound("activity.create", {
                "assigned_agent_id": "codex",
                "task_description": "review code",
            })
        assert result["activity_id"] == "act-123"

    def test_create_uses_assigned_agent_as_agent_id(self):
        from app.services import inbound_webhook_service as svc
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return {"id": "act-1", "status": "active"}

        with patch("app.services.inbound_webhook_service.activity_service.create_activity", side_effect=fake_create):
            svc.handle_inbound("activity.create", {"assigned_agent_id": "codex"})

        assert captured["agent_id"] == "codex"
        assert captured["user_id"] == "inbound-webhook"


class TestActivityAssign:
    def test_assign_requires_assigned_agent_id(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="assigned_agent_id"):
            svc.handle_inbound("activity.assign", {"activity_id": "act-1"})

    def test_assign_calls_reassign(self):
        from app.services import inbound_webhook_service as svc
        reassigned = {}

        def fake_reassign(activity_id, new_agent_id):
            reassigned["activity_id"] = activity_id
            reassigned["new_agent_id"] = new_agent_id
            return {"id": activity_id}

        with patch("app.services.inbound_webhook_service.activity_service.reassign_activity", side_effect=fake_reassign), \
             patch("app.services.inbound_webhook_service.activity_service.update_activity"):
            svc.handle_inbound("activity.assign", {"activity_id": "act-1", "assigned_agent_id": "opus"})

        assert reassigned == {"activity_id": "act-1", "new_agent_id": "opus"}


class TestActivityUpdate:
    def test_update_requires_at_least_one_field(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="at least one"):
            svc.handle_inbound("activity.update", {"activity_id": "act-1"})

    def test_update_calls_update_activity(self):
        from app.services import inbound_webhook_service as svc
        updated = {}

        def fake_update(activity_id, **kwargs):
            updated["id"] = activity_id
            updated.update(kwargs)

        with patch("app.services.inbound_webhook_service.activity_service.update_activity", side_effect=fake_update):
            svc.handle_inbound(
                "activity.update",
                {"activity_id": "act-1", "status": "completed", "task_note": "Handled the request"},
            )

        assert updated["id"] == "act-1"
        assert updated["status"] == "completed"
        assert updated["task_note"] == "Handled the request"


class TestActivityCancel:
    def test_cancel_calls_cancel_activity(self):
        from app.services import inbound_webhook_service as svc
        cancelled = {}

        def fake_cancel(activity_id):
            cancelled["id"] = activity_id

        with patch("app.services.inbound_webhook_service.activity_service.cancel_activity", side_effect=fake_cancel):
            result = svc.handle_inbound("activity.cancel", {"activity_id": "act-1"})

        assert cancelled["id"] == "act-1"
        assert result["status"] == "cancelled"


class TestActivityNote:
    def test_note_requires_non_empty_note(self):
        from app.services import inbound_webhook_service as svc
        with pytest.raises(ValueError, match="non-empty note"):
            svc.handle_inbound("activity.note", {"activity_id": "act-1", "note": ""})

    def test_note_writes_audit_not_activity(self):
        from app.services import inbound_webhook_service as svc
        audit_calls = []

        def fake_audit(**kwargs):
            audit_calls.append(kwargs)

        with patch("app.services.inbound_webhook_service.audit_service.write_event", side_effect=fake_audit):
            result = svc.handle_inbound("activity.note", {"activity_id": "act-1", "note": "handoff complete"})

        assert result["noted"] is True
        assert any(c["action"] == "inbound_webhook_note" for c in audit_calls)

    def test_note_does_not_call_activity_service(self):
        from app.services import inbound_webhook_service as svc
        with patch("app.services.inbound_webhook_service.activity_service") as mock_svc, \
             patch("app.services.inbound_webhook_service.audit_service.write_event"):
            svc.handle_inbound("activity.note", {"activity_id": "act-1", "note": "done"})
        mock_svc.create_activity.assert_not_called()
        mock_svc.update_activity.assert_not_called()
        mock_svc.cancel_activity.assert_not_called()
