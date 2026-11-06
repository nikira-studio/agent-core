import atexit
import os
import shutil
import tempfile

import pytest

from app.branding import ENV_PREFIX

# Anything that touches the data directory before a test sets its own (an import, a collected
# module, a fixture-less test) must land in a scratch directory, never in ./data in the checkout,
# where it would leave a database or a broker credential next to the source tree.
_SUITE_DATA_DIR = tempfile.mkdtemp(prefix="agent-core-suite-")
os.environ[f"{ENV_PREFIX}DATA_PATH"] = _SUITE_DATA_DIR
from app.config import settings as _suite_settings  # noqa: E402  (already imported by the app package)

_suite_settings.DATA_PATH = _SUITE_DATA_DIR
atexit.register(shutil.rmtree, _SUITE_DATA_DIR, ignore_errors=True)


_original_settings = {}


@pytest.fixture(scope="function")
def test_db_path():
    import tempfile
    from pathlib import Path
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir) / "test.db"
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="function")
def clean_db(test_db_path):
    import app.database as db_mod
    db_mod.DB_PATH_OVERRIDE = str(test_db_path)
    os.environ[f"{ENV_PREFIX}TEST_DB"] = str(test_db_path)

    tmpdir = test_db_path.parent
    from app.config import settings

    _original_settings["DATA_PATH"] = settings.DATA_PATH
    settings.DATA_PATH = str(tmpdir)
    os.environ[f"{ENV_PREFIX}DATA_PATH"] = str(tmpdir)
    try:
        from app.security import encryption
        encryption._fernet = None
        encryption._keyring = None
    except Exception:
        pass
    from app.services import auth_service as _auth

    _auth.reset_session_key_cache()

    import shutil as si
    for f in tmpdir.iterdir():
        try:
            si.unlink(f)
        except Exception:
            pass

    if test_db_path.exists():
        test_db_path.unlink()
    from app.database import get_db, init_db
    init_db()
    # Most tests talk to local stub servers, so they run with private addresses allowed;
    # the fresh-install default (block) and the report-only mode have their own tests.
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('internal_hosts_mode', 'allow')")
        conn.commit()
    from app.services import network_policy_service
    network_policy_service.reset_cache()

    yield test_db_path

    db_mod.DB_PATH_OVERRIDE = None
    os.environ.pop(f"{ENV_PREFIX}TEST_DB", None)
    try:
        from app.security import encryption
        encryption._fernet = None
        encryption._keyring = None
    except Exception:
        pass
    if "DATA_PATH" in _original_settings:
        settings.DATA_PATH = _original_settings.pop("DATA_PATH")


@pytest.fixture(autouse=True)
def _fast_password_hashing(monkeypatch):
    # Production cost (12) makes every register/login about half a second;
    # the tests check behaviour, not hash strength.
    from app.services import auth_service

    monkeypatch.setattr(auth_service, "BCRYPT_COST", 4)


@pytest.fixture(autouse=True)
def reset_runtime_state():
    from app.security.rate_limiter import RateLimiter, ConcurrentSearchGuard

    RateLimiter.reset()
    ConcurrentSearchGuard.reset()
    yield
    RateLimiter.reset()
    ConcurrentSearchGuard.reset()


@pytest.fixture(scope="function")
def test_client(clean_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="function")
def admin_token(test_client):
    r = test_client.post("/api/auth/register", json={
        "email": "admin@test.local",
        "password": "testpassword123",
        "display_name": "Admin Test",
    })
    assert r.status_code == 200, f"register failed: {r.json()}"
    r2 = test_client.post("/api/auth/login", json={
        "email": "admin@test.local",
        "password": "testpassword123",
    })
    assert r2.status_code == 200, f"login failed: {r2.json()}"
    # A login session is a Bearer key only on /api/auth/*; tests act as the
    # admin everywhere, which is what an operator token is for.
    from app.services.auth_service import create_operator_token

    token, _ = create_operator_token(r2.json()["data"]["user_id"], 60, "tests")
    return token


@pytest.fixture(scope="function")
def agent_token(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "testagent", "display_name": "Test Agent", "description": "Test"},
    )
    assert r.status_code in (200, 201), f"agent create failed: {r.json()}"
    key_response = test_client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "testagent", "output_type": "env"},
    )
    assert key_response.status_code == 200, f"agent key generate failed: {key_response.json()}"
    return key_response.json()["data"]["api_key"]


def operator_token_for(user_id: str) -> str:
    """A Bearer token that acts as `user_id` on any route (login sessions are
    Bearer keys only on /api/auth/*)."""
    from app.services.auth_service import create_operator_token

    return create_operator_token(user_id, 60, "tests")[0]


def csrf_headers(client) -> dict:
    """The CSRF header for state-changing requests a test client makes with its
    session cookie (dashboard pages add it to every fetch)."""
    from app.services.auth_service import CSRF_HEADER, csrf_token_for_session_token

    token = csrf_token_for_session_token(client.cookies.get("session_token") or "")
    return {CSRF_HEADER: token} if token else {}
