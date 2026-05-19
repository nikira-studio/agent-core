"""Integration tests for the inbound webhook receiver."""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def inbound_key(admin_client):
    r = admin_client.post("/api/webhooks/inbound/key")
    assert r.status_code == 201
    return r.json()["data"]["key"]


# ---------------------------------------------------------------------------
# Key management (admin API)
# ---------------------------------------------------------------------------

class TestInboundKeyManagement:
    def test_key_status_no_key(self, admin_client):
        r = admin_client.get("/api/webhooks/inbound/key/status")
        assert r.status_code == 200
        assert r.json()["data"]["has_key"] is False

    def test_generate_key_returns_plaintext(self, admin_client):
        r = admin_client.post("/api/webhooks/inbound/key")
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["key"].startswith("ac_inbound_")
        assert "note" in data

    def test_generate_key_status_after_generate(self, admin_client, inbound_key):
        r = admin_client.get("/api/webhooks/inbound/key/status")
        assert r.json()["data"]["has_key"] is True

    def test_generate_key_twice_is_409(self, admin_client, inbound_key):
        r = admin_client.post("/api/webhooks/inbound/key")
        assert r.status_code == 409

    def test_rotate_key_succeeds(self, admin_client, inbound_key):
        r = admin_client.post("/api/webhooks/inbound/key/rotate")
        assert r.status_code == 200
        new_key = r.json()["data"]["key"]
        assert new_key.startswith("ac_inbound_")
        assert new_key != inbound_key

    def test_rotate_invalidates_old_key(self, admin_client, inbound_key):
        admin_client.post("/api/webhooks/inbound/key/rotate")
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create", "assigned_agent_id": "codex"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 401

    def test_rotate_without_key_is_404(self, admin_client):
        r = admin_client.post("/api/webhooks/inbound/key/rotate")
        assert r.status_code == 404

    def test_non_admin_cannot_generate_key(self, user_client):
        r = user_client.post("/api/webhooks/inbound/key")
        assert r.status_code == 403

    def test_non_admin_cannot_rotate_key(self, user_client):
        r = user_client.post("/api/webhooks/inbound/key/rotate")
        assert r.status_code == 403

    def test_non_admin_cannot_view_key_status(self, user_client):
        r = user_client.get("/api/webhooks/inbound/key/status")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Auth on the inbound receive endpoint
# ---------------------------------------------------------------------------

class TestInboundAuth:
    def test_missing_key_is_rejected(self, test_client, clean_db):
        r = test_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create", "assigned_agent_id": "codex"},
        )
        assert r.status_code == 401

    def test_wrong_key_is_rejected(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create", "assigned_agent_id": "codex"},
            headers={"X-Agent-Core-Inbound-Key": "totally-wrong-key"},
        )
        assert r.status_code == 401

    def test_valid_key_is_accepted(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create", "assigned_agent_id": "codex", "task_description": "test"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# activity.create
# ---------------------------------------------------------------------------

class TestInboundActivityCreate:
    def test_create_missing_agent_is_400(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 400

    def test_create_makes_activity(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={
                "event_type": "activity.create",
                "assigned_agent_id": "codex",
                "task_description": "Review webhook implementation",
                "workspace": "workspace:agent-core",
            },
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert "activity_id" in data
        assert data["activity_id"] is not None

    def test_create_activity_appears_in_list(self, admin_client, inbound_key):
        admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.create", "assigned_agent_id": "codex", "task_description": "from inbound"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        from app.services import activity_service
        activities = activity_service.list_activities()
        descriptions = [a.get("task_description") for a in activities]
        assert "from inbound" in descriptions


# ---------------------------------------------------------------------------
# activity.assign
# ---------------------------------------------------------------------------

class TestInboundActivityAssign:
    def _make_activity(self):
        from app.services import activity_service
        return activity_service.create_activity(
            agent_id="codex", user_id="admin", task_description="initial", memory_scope=None
        )

    def test_assign_missing_activity_id_is_400(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.assign", "assigned_agent_id": "opus"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 400

    def test_assign_missing_agent_id_is_400(self, admin_client, inbound_key):
        act = self._make_activity()
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.assign", "activity_id": act["id"]},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 400

    def test_assign_updates_agent(self, admin_client, inbound_key):
        act = self._make_activity()
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.assign", "activity_id": act["id"], "assigned_agent_id": "opus"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 200
        assert r.json()["data"]["assigned_agent_id"] == "opus"


# ---------------------------------------------------------------------------
# activity.cancel
# ---------------------------------------------------------------------------

class TestInboundActivityCancel:
    def test_cancel_sets_status(self, admin_client, inbound_key):
        from app.services import activity_service
        act = activity_service.create_activity(
            agent_id="codex", user_id="admin", task_description="to cancel", memory_scope=None
        )
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.cancel", "activity_id": act["id"], "reason": "no longer needed"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# activity.note
# ---------------------------------------------------------------------------

class TestInboundActivityNote:
    def test_note_empty_is_400(self, admin_client, inbound_key):
        from app.services import activity_service
        act = activity_service.create_activity(
            agent_id="codex", user_id="admin", task_description="noted", memory_scope=None
        )
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.note", "activity_id": act["id"], "note": ""},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 400

    def test_note_writes_audit_entry(self, admin_client, inbound_key):
        from app.services import activity_service, audit_service
        act = activity_service.create_activity(
            agent_id="codex", user_id="admin", task_description="noted", memory_scope=None
        )
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.note", "activity_id": act["id"], "note": "handoff complete"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 200
        assert r.json()["data"]["noted"] is True

        events = audit_service.query_events(action="inbound_webhook_note")
        assert any(e["resource_id"] == act["id"] for e in events)


# ---------------------------------------------------------------------------
# Unknown event type
# ---------------------------------------------------------------------------

class TestInboundUnknownEvent:
    def test_unknown_event_is_400(self, admin_client, inbound_key):
        r = admin_client.post(
            "/api/webhooks/inbound",
            json={"event_type": "activity.delete"},
            headers={"X-Agent-Core-Inbound-Key": inbound_key},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------

class TestInboundDashboard:
    def test_webhooks_page_has_inbound_section(self, admin_client):
        r = admin_client.get("/webhooks")
        assert r.status_code == 200
        assert "Inbound Receiver" in r.text
        assert "X-Agent-Core-Inbound-Key" in r.text

    def test_webhooks_page_shows_generate_button_when_no_key(self, admin_client):
        r = admin_client.get("/webhooks")
        assert "generateInboundKey" in r.text

    def test_webhooks_page_shows_rotate_button_after_key_generated(self, admin_client, inbound_key):
        r = admin_client.get("/webhooks")
        assert "rotateInboundKey" in r.text
