"""Hardening: sessions have their own signing key; sign-in limits are shared."""

import os
import secrets
import stat

import jwt

from app.config import settings
from app.security.rate_limiter import RL
from app.services import auth_service


_PW = secrets.token_hex(12)  # a throwaway login for these tests


def _user(user_id="keyuser"):
    auth_service.create_user(user_id=user_id, email=f"{user_id}@test.local",
                             password=_PW, display_name="Key User", role="admin")
    return auth_service.create_session(user_id)


def test_session_key_is_its_own_private_file(clean_db):
    _user()
    path = settings.data_dir / auth_service.SESSION_KEY_FILENAME
    key = path.read_bytes()
    assert len(key) == 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    from app.security.encryption import get_keyring
    assert all(key != k for k in get_keyring())


def test_token_signed_with_the_credential_key_is_refused(clean_db):
    """The old scheme signed sessions with the credential key; such tokens no longer work."""
    from app.security.encryption import get_primary_key

    session = _user()
    session_id = auth_service.decode_jwt(session["session_id"])
    forged = jwt.encode({"sub": session_id}, get_primary_key(), algorithm="HS256")
    assert auth_service.decode_jwt(forged) is None
    assert auth_service.validate_session(forged) is None
    assert auth_service.validate_session(session["session_id"]) is not None


def test_replacing_the_session_key_signs_everyone_out(clean_db):
    session = _user()
    (settings.data_dir / auth_service.SESSION_KEY_FILENAME).unlink()
    auth_service.reset_session_key_cache()
    try:
        assert auth_service.validate_session(session["session_id"]) is None
    finally:
        auth_service.reset_session_key_cache()


def test_key_status_shows_a_digest_not_the_key(clean_db):
    from app.security.encryption import get_keyring
    from app.services.credential_rotation_service import get_key_status

    get_keyring()
    status = get_key_status()
    primary = get_keyring()[0].decode()
    assert len(status["primary_key_id"]) == 12
    assert status["primary_key_id"] not in primary


def test_failed_login_limit_is_kept_in_the_database(clean_db, monkeypatch):
    import time as real_time

    from app.database import get_db
    from app.security import rate_limiter

    # A bucket nobody has touched reports its "reset" as the current second, so comparing two peeks taken
    # across a second boundary failed now and then under load. Freeze the clock so the comparison is exact.
    frozen = real_time.time()
    monkeypatch.setattr(rate_limiter.time, "time", lambda: frozen)

    for _ in range(10):
        assert RL.check("user", "198.51.100.7", "login_failed")[0] is True
    assert RL.check("user", "198.51.100.7", "login_failed")[0] is False
    assert RL.peek("user", "198.51.100.7", "login_failed")[0] is False
    with get_db() as conn:
        row = conn.execute("SELECT tokens FROM rate_limit_buckets WHERE bucket_key = ?",
                           ("login_failed:user:198.51.100.7",)).fetchone()
    assert row is not None and row["tokens"] < 1
    # Another address has its own bucket, and peeking takes nothing.
    untouched = RL.peek("user", "198.51.100.8", "login_failed")
    assert untouched[0] is True and untouched == RL.peek("user", "198.51.100.8", "login_failed")
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rate_limit_buckets WHERE bucket_key LIKE '%198.51.100.8'").fetchone()[0] == 0


def test_shared_limit_refills_over_time(clean_db, monkeypatch):
    import time as real_time

    from app.security import rate_limiter

    start = real_time.time()
    monkeypatch.setattr(rate_limiter.time, "time", lambda: start)
    for _ in range(5):
        RL.check("user", "otp-user", "otp_failed")
    assert RL.check("user", "otp-user", "otp_failed")[0] is False
    monkeypatch.setattr(rate_limiter.time, "time", lambda: start + 301)
    assert RL.check("user", "otp-user", "otp_failed")[0] is True


def test_reset_clears_shared_buckets(clean_db):
    for _ in range(11):
        RL.check("user", "203.0.113.9", "login_failed")
    RL.reset()
    assert RL.check("user", "203.0.113.9", "login_failed")[0] is True


def test_client_ip_reads_forwarded_for_right_to_left(monkeypatch):
    from types import SimpleNamespace

    from app.routes.auth import get_client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "10.0.0.2,10.0.0.3")

    def req(direct, xff=None):
        return SimpleNamespace(client=SimpleNamespace(host=direct),
                               headers={"X-Forwarded-For": xff} if xff else {})

    # A client-written leftmost entry is ignored; the proxy's appended hop counts.
    assert get_client_ip(req("10.0.0.2", "1.2.3.4, 198.51.100.9")) == "198.51.100.9"
    # Chained trusted proxies are skipped.
    assert get_client_ip(req("10.0.0.2", "1.2.3.4, 198.51.100.9, 10.0.0.3")) == "198.51.100.9"
    # Untrusted direct callers can't use the header at all.
    assert get_client_ip(req("203.0.113.5", "1.2.3.4")) == "203.0.113.5"
    # A trusted proxy with no header is the caller.
    assert get_client_ip(req("10.0.0.2")) == "10.0.0.2"


def test_peek_of_an_untouched_bucket_reports_the_current_second_as_its_reset(clean_db, monkeypatch):
    """Pins why the comparison above needs a frozen clock: the answer moves with the wall clock, by design."""
    import time as real_time

    from app.security import rate_limiter

    now = real_time.time()
    monkeypatch.setattr(rate_limiter.time, "time", lambda: now)
    first = RL.peek("user", "198.51.100.9", "login_failed")
    monkeypatch.setattr(rate_limiter.time, "time", lambda: now + 1)
    second = RL.peek("user", "198.51.100.9", "login_failed")
    assert first[0] is True and second[0] is True
    assert second[1]["reset"] == first[1]["reset"] + 1 and second[1]["remaining"] == first[1]["remaining"]
