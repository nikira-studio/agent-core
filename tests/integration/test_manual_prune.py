"""Integration tests for manual audit/activity pruning."""
import pytest


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


class TestManualPrune:
    def test_prune_audit_rows_before_cutoff(self, admin_client):
        from app.services import audit_service
        from app.database import get_db

        audit_service.write_event(
            actor_type="user",
            actor_id="admin",
            action="session_login",
            result="success",
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE audit_log SET timestamp = '2000-01-01 00:00:00' WHERE action = 'session_login'"
            )
            conn.commit()

        r = admin_client.post(
            "/api/dashboard/prune",
            json={"resource_type": "audit", "before_date": "2001-01-01"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["deleted_count"] >= 1

        with get_db() as conn:
            remaining = conn.execute(
                "SELECT 1 FROM audit_log WHERE action = 'session_login'"
            ).fetchone()
        assert remaining is None

    def test_prune_activity_keeps_active_rows(self, admin_client):
        from app.services import activity_service
        from app.database import get_db

        old_done = activity_service.create_activity(
            agent_id="codex",
            user_id="admin",
            task_description="old completed",
            memory_scope="workspace:agent-core",
        )
        activity_service.update_activity(old_done["id"], status="completed")
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_activity SET ended_at = '2000-01-01T00:00:00+00:00', updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (old_done["id"],),
            )

        active = activity_service.create_activity(
            agent_id="codex",
            user_id="admin",
            task_description="still active",
            memory_scope="workspace:agent-core",
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_activity SET started_at = '2000-01-01T00:00:00+00:00', updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (active["id"],),
            )
            conn.commit()

        r = admin_client.post(
            "/api/dashboard/prune",
            json={"resource_type": "activity", "before_date": "2001-01-01"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["deleted_count"] >= 1

        with get_db() as conn:
            deleted = conn.execute(
                "SELECT 1 FROM agent_activity WHERE id = ?",
                (old_done["id"],),
            ).fetchone()
            still_there = conn.execute(
                "SELECT 1 FROM agent_activity WHERE id = ?",
                (active["id"],),
            ).fetchone()
        assert deleted is None
        assert still_there is not None

    def test_prune_is_admin_only(self, user_client):
        r = user_client.post(
            "/api/dashboard/prune",
            json={"resource_type": "audit", "before_date": "2001-01-01"},
        )
        assert r.status_code in (302, 403)
