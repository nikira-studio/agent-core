import os
import pytest


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
    os.environ["AGENT_CORE_TEST_DB"] = str(test_db_path)

    tmpdir = test_db_path.parent
    from app.config import settings

    _original_settings["DATA_PATH"] = settings.DATA_PATH
    settings.DATA_PATH = str(tmpdir)
    os.environ["AGENT_CORE_DATA_PATH"] = str(tmpdir)
    try:
        from app.security import encryption
        encryption._fernet = None
        encryption._keyring = None
    except Exception:
        pass

    import shutil as si
    for f in tmpdir.iterdir():
        try:
            si.unlink(f)
        except Exception:
            pass

    if test_db_path.exists():
        test_db_path.unlink()
    from app.database import init_db
    init_db()

    yield test_db_path

    db_mod.DB_PATH_OVERRIDE = None
    os.environ.pop("AGENT_CORE_TEST_DB", None)
    try:
        from app.security import encryption
        encryption._fernet = None
        encryption._keyring = None
    except Exception:
        pass
    if "DATA_PATH" in _original_settings:
        settings.DATA_PATH = _original_settings.pop("DATA_PATH")


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
    return TestClient(app)


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
    return r2.json()["data"]["session_id"]


@pytest.fixture(scope="function")
def agent_token(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "testagent", "display_name": "Test Agent", "description": "Test"},
    )
    assert r.status_code in (200, 201), f"agent create failed: {r.json()}"
    key_response = test_client.post(
        "/api/agent-setup/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "testagent", "output_type": "env"},
    )
    assert key_response.status_code == 200, f"agent key generate failed: {key_response.json()}"
    return key_response.json()["data"]["api_key"]
