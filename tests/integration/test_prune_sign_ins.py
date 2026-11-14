"""Dead sessions and spent operator tokens clean themselves up in the maintenance run."""

from datetime import timedelta

from app.database import get_db
from app.services import auth_service, backup_service
from app.time_utils import utc_now


def _owner():
    with get_db() as conn:
        return conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]


def _session(sid, user, *, expires_in_hours, idle_minutes, channel="dashboard"):
    now = utc_now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, created_at, last_activity, channel) VALUES (?,?,?,?,?,?)",
            (sid, user, (now + timedelta(hours=expires_in_hours)).isoformat(), now.isoformat(),
             (now - timedelta(minutes=idle_minutes)).isoformat(), channel),
        )
        conn.commit()


def _ids(table):
    with get_db() as conn:
        return {r[0] for r in conn.execute(f"SELECT id FROM {table}").fetchall()}


def test_dead_sessions_go_and_live_ones_stay(test_client, admin_token):
    user = _owner()
    _session("live", user, expires_in_hours=4, idle_minutes=5)
    _session("expired", user, expires_in_hours=-1, idle_minutes=5)
    _session("idle", user, expires_in_hours=4, idle_minutes=600)
    _session("half-signed-in-expired", user, expires_in_hours=-2, idle_minutes=0, channel="pending_otp")
    _session("half-signed-in-open", user, expires_in_hours=1, idle_minutes=600, channel="pending_otp")
    out = auth_service.prune_sign_ins()
    assert out["sessions_pruned"] == 3
    left = _ids("sessions")
    assert {"live", "half-signed-in-open"} <= left
    assert not ({"expired", "idle", "half-signed-in-expired"} & left)


def test_expired_operator_tokens_read_as_revoked_and_old_rows_are_pruned(test_client, admin_token):
    user = _owner()
    token, rec = auth_service.create_operator_token(user, minutes=5, label="soon-expired")
    _, live = auth_service.create_operator_token(user, minutes=30, label="still-live")
    _, revoked = auth_service.create_operator_token(user, minutes=30, label="revoked-by-person")
    assert auth_service.revoke_operator_token(user, revoked["id"])
    now = utc_now()
    with get_db() as conn:
        conn.execute("UPDATE operator_tokens SET expires_at = ? WHERE id = ?",
                     ((now - timedelta(hours=3)).isoformat(), rec["id"]))
        # an old, long-expired, revoked row beyond the audit window
        conn.execute(
            "INSERT INTO operator_tokens (id, user_id, token_hash, label, created_at, expires_at, revoked_at) "
            "VALUES ('ancient', ?, 'h-ancient', 'old', ?, ?, ?)",
            (user, (now - timedelta(days=500)).isoformat(), (now - timedelta(days=499)).isoformat(),
             (now - timedelta(days=499)).isoformat()),
        )
        conn.commit()
    assert auth_service.validate_operator_token(token) is None
    out = auth_service.prune_sign_ins()
    assert out["operator_tokens_closed"] == 1 and out["operator_tokens_pruned"] == 1
    with get_db() as conn:
        row = conn.execute("SELECT revoked_at, expires_at FROM operator_tokens WHERE id = ?", (rec["id"],)).fetchone()
        assert row["revoked_at"] == row["expires_at"]  # dated at its expiry
        assert conn.execute("SELECT revoked_at FROM operator_tokens WHERE id = ?", (live["id"],)).fetchone()["revoked_at"] is None
        assert conn.execute("SELECT 1 FROM operator_tokens WHERE id = 'ancient'").fetchone() is None
        assert conn.execute("SELECT 1 FROM operator_tokens WHERE id = ?", (revoked["id"],)).fetchone() is not None  # recent history stays
    labels = [t["label"] for t in auth_service.list_operator_tokens(user)]
    assert "still-live" in labels and "soon-expired" not in labels and "revoked-by-person" not in labels


def test_the_maintenance_run_does_it_and_reports_counts(test_client, admin_token):
    user = _owner()
    _session("old-one", user, expires_in_hours=-5, idle_minutes=5)
    result = backup_service.run_scheduled_maintenance()
    assert result["sessions_pruned"] >= 1
    assert "operator_tokens_closed" in result and "operator_tokens_pruned" in result
    assert "old-one" not in _ids("sessions")
