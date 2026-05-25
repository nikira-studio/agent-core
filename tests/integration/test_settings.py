import pytest

from app.branding import APP_NAME, CREDENTIAL_PREFIX


@pytest.fixture
def authenticated_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("testuser", "user@test.local", "testpassword123", "Test User", "user")
    session = create_session("testuser", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


def test_settings_page_loads(authenticated_client):
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Change Password" in html
    assert "Manage OTP" in html
    assert "Backup Codes" not in html


def test_non_admin_settings_hides_admin_controls(authenticated_client):
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Rotate Broker Credential" not in html
    assert "Backup & Restore" not in html
    assert "Encryption Key" not in html


def test_admin_settings_exposes_encryption_key_and_restore_mode_controls(admin_client):
    r = admin_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Secrets Snapshot" in html
    assert "Credential Entries" in html
    assert "This key protects stored credential values" in html
    assert f"{CREDENTIAL_PREFIX}* references at runtime" in html
    assert "Rotate Key" in html
    assert "Restore Key" in html
    assert 'id="restore-mode"' in html
    assert 'value="merge"' in html
    assert "/api/credentials/rotate" in html
    assert "/api/credentials/restore-key" in html
    assert 'id="backup-otp"' not in html
    assert 'id="restore-otp"' not in html
    assert 'id="credential-key-rotate-otp"' not in html
    assert 'id="credential-key-restore-otp"' not in html


def test_admin_settings_exposes_real_system_behavior_controls(admin_client):
    r = admin_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Scratchpad Retention" in html
    assert "scratchpad-retention-days" in html
    assert "solo-mode-enabled" in html
    assert "/api/dashboard/system-settings" in html
    assert "Run Maintenance" in html
    assert "Scratchpad memories pruned" in html


def test_admin_settings_exposes_vector_controls(admin_client):
    r = admin_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert 'id="vector-provider"' in html
    assert '<option value="ollama" selected>Ollama</option>' in html
    assert '<option value="generic" ' in html
    assert 'id="vector-model-select"' in html
    assert 'window.saveVectorSettings = saveVectorSettings' in html
    assert 'window.testVectorSettings = testVectorSettings' in html
    assert 'id="vector-auth-type"' in html
    assert 'id="vector-api-key"' in html
    assert "testVectorSettings()" in html
    assert "vector_provider: provider" in html


def test_vector_settings_api_saves_generic_auth_settings(admin_client):
    r = admin_client.post(
        "/api/dashboard/vector-settings",
        json={
            "vector_search_enabled": "true",
            "vector_provider": "generic",
            "vector_model": "nomic-embed-text",
            "vector_url": "http://localhost:11434",
            "vector_dimension": "768",
            "vector_auth_type": "bearer",
            "vector_api_key": "test-key",
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]["settings"]
    assert data["vector_search_enabled"] == "true"
    assert data["vector_provider"] == "generic"
    assert data["vector_model"] == "nomic-embed-text"
    assert data["vector_url"] == "http://localhost:11434"
    assert data["vector_dimension"] == "768"
    assert data["vector_auth_type"] == "bearer"
    assert data["vector_has_api_key"] is True
    assert data["vector_api_key"] == "test-key"


def test_vector_settings_models_rejects_invalid_url(admin_client):
    r = admin_client.get("/api/dashboard/vector-settings/models?url=not-a-url")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_URL"


def test_vector_settings_models_lists_ollama_models(admin_client, monkeypatch):
    import app.routes.dashboard as dashboard

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {"name": "nomic-embed-text:latest"},
                    {"name": "mxbai-embed-large:latest"},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url == "http://ollama.test/api/tags"
            return FakeResponse()

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", FakeClient)

    r = admin_client.get(
        "/api/dashboard/vector-settings/models?url=http%3A%2F%2Follama.test"
    )
    assert r.status_code == 200
    assert r.json()["data"]["models"] == [
        "nomic-embed-text:latest",
        "mxbai-embed-large:latest",
    ]


def test_settings_password_page_loads(authenticated_client):
    r = authenticated_client.get("/settings/password")
    assert r.status_code == 200
    html = r.text
    assert "Current Password" in html
    assert "New Password" in html


def test_settings_otp_page_loads(authenticated_client):
    r = authenticated_client.get("/settings/otp")
    assert r.status_code == 200
    html = r.text
    assert "Two-Factor" in html or "OTP" in html
    assert "startOtpEnrollment" in html
    assert "Preparing OTP setup" in html
    assert "QR code could not be generated" in html
    assert "backup code" not in html


def test_login_otp_page_accepts_otp_only_input(test_client, clean_db):
    r = test_client.get("/otp")
    assert r.status_code == 200
    html = r.text
    assert "backup code" not in html
    assert 'autocomplete="one-time-code"' in html
    assert 'name="otp_code"' in html
    assert 'name="otp_code" placeholder="123456" maxlength="6"' not in html


def test_verify_otp_accepts_current_code(authenticated_client):
    import pyotp
    from app.services.auth_service import enroll_otp, confirm_otp_enrollment, verify_otp

    pending = enroll_otp("testuser")
    assert confirm_otp_enrollment("testuser", pyotp.TOTP(pending["secret"]).now())
    current_code = pyotp.TOTP(pending["secret"]).now()

    assert verify_otp("testuser", current_code) is True


def test_settings_password_requires_auth(test_client, clean_db):
    r = test_client.get("/settings/password", follow_redirects=False)
    assert r.status_code == 302


def test_settings_otp_requires_auth(test_client, clean_db):
    r = test_client.get("/settings/otp", follow_redirects=False)
    assert r.status_code == 302


def test_first_run_creates_admin_account(test_client, clean_db):
    r = test_client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert (
        f"Welcome to {APP_NAME}" in html
        or "Create your admin account" in html
        or "Sign In" in html
    )


def test_first_run_browser_path_reaches_otp_and_dashboard(test_client, clean_db):
    import pyotp

    r = test_client.post(
        "/api/auth/register",
        json={
            "email": "admin@test.local",
            "password": "testpassword123",
            "display_name": "Admin Test",
        },
    )
    assert r.status_code == 200
    assert "session_token=" in r.headers.get("set-cookie", "")

    otp_page = test_client.get("/settings/otp")
    assert otp_page.status_code == 200
    assert "/static/js/dashboard.js?v=" in otp_page.text
    assert "escapeHtml(j.data.secret)" in otp_page.text
    dashboard_js = test_client.get("/static/js/dashboard.js")
    assert dashboard_js.status_code == 200
    assert "function escapeHtml" in dashboard_js.text

    enroll = test_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123"},
    )
    assert enroll.status_code == 200
    data = enroll.json()["data"]
    assert data["otp_uri"]
    assert data["qr_svg"].startswith("data:image/svg+xml;base64,")

    confirm = test_client.post(
        "/api/auth/otp/confirm",
        json={"otp_code": pyotp.TOTP(data["secret"]).now()},
    )
    assert confirm.status_code == 200, confirm.json()
    assert confirm.json()["data"]["message"] == "OTP enabled"

    dashboard = test_client.get("/")
    assert dashboard.status_code == 200


def test_otp_enrollment_requires_code_confirmation(authenticated_client):
    import pyotp
    from app.services.auth_service import is_otp_enrolled

    missing_password = authenticated_client.post("/api/auth/otp/enroll", json={})
    assert missing_password.status_code == 422

    wrong_password = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "wrongpassword"},
    )
    assert wrong_password.status_code == 403

    enroll = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123"},
    )
    assert enroll.status_code == 200
    data = enroll.json()["data"]
    assert is_otp_enrolled("testuser") is False

    bad = authenticated_client.post(
        "/api/auth/otp/confirm", json={"otp_code": "000000"}
    )
    assert bad.status_code == 401
    assert is_otp_enrolled("testuser") is False

    good = authenticated_client.post(
        "/api/auth/otp/confirm",
        json={"otp_code": pyotp.TOTP(data["secret"]).now()},
    )
    assert good.status_code == 200, good.json()
    assert is_otp_enrolled("testuser") is True
    assert good.json()["data"]["message"] == "OTP enabled"


def test_otp_reset_keeps_existing_otp_active_until_confirmation(authenticated_client):
    import pyotp
    from app.services.auth_service import (
        enroll_otp,
        confirm_otp_enrollment,
        is_otp_enrolled,
        verify_otp,
    )

    original = enroll_otp("testuser")
    original_code = pyotp.TOTP(original["secret"]).now()
    assert confirm_otp_enrollment("testuser", original_code)
    assert is_otp_enrolled("testuser") is True

    reset_without_otp = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123"},
    )
    assert reset_without_otp.status_code == 400

    reset_wrong_otp = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123", "otp_code": "000000"},
    )
    assert reset_wrong_otp.status_code == 403

    pending = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123", "otp_code": original_code},
    )
    assert pending.status_code == 200
    assert is_otp_enrolled("testuser") is True
    assert verify_otp("testuser", original_code) is True

    confirm = authenticated_client.post(
        "/api/auth/otp/confirm",
        json={"otp_code": pyotp.TOTP(pending.json()["data"]["secret"]).now()},
    )
    assert confirm.status_code == 200, confirm.json()
    assert is_otp_enrolled("testuser") is True

    page = authenticated_client.get("/settings/otp")
    assert page.status_code == 200
    assert "submitOtpReset(event)" in page.text
    assert "submitOtpDisable(event)" in page.text
    assert "otp-reset-result" in page.text
    assert "startOtpEnrollment('otp-reset-result')" in page.text


def test_otp_disable_allows_reenroll(authenticated_client):
    import pyotp
    from app.services.auth_service import enroll_otp, confirm_otp_enrollment, is_otp_enrolled

    pending = enroll_otp("testuser")
    secret = pending["secret"]
    assert confirm_otp_enrollment("testuser", pyotp.TOTP(secret).now())
    assert is_otp_enrolled("testuser") is True

    disable = authenticated_client.post(
        "/api/auth/otp/disable",
        json={
            "current_password": "testpassword123",
            "otp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert disable.status_code == 200, disable.json()
    assert is_otp_enrolled("testuser") is False

    reenroll = authenticated_client.post(
        "/api/auth/otp/enroll",
        json={"current_password": "testpassword123"},
    )
    assert reenroll.status_code == 200
    assert "secret" in reenroll.json()["data"]


def test_change_password_endpoint(authenticated_client):
    r = authenticated_client.post(
        "/api/auth/password",
        json={"current_password": "testpassword123", "new_password": "newpassword456"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_change_password_wrong_current_fails(authenticated_client):
    r = authenticated_client.post(
        "/api/auth/password",
        json={"current_password": "wrongpassword", "new_password": "newpassword456"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_admin_can_create_and_update_user(admin_client):
    create = admin_client.post(
        "/api/auth/users",
        json={
            "email": "newuser@test.local",
            "password": "testpassword123",
            "display_name": "New User",
            "role": "user",
        },
    )
    assert create.status_code == 201, create.json()
    user_id = create.json()["data"]["user"]["id"]

    update = admin_client.put(
        f"/api/auth/users/{user_id}",
        json={
            "email": "renamed@test.local",
            "display_name": "Renamed User",
            "role": "admin",
        },
    )
    assert update.status_code == 200, update.json()

    from app.services.auth_service import get_user_by_id

    user = get_user_by_id(user_id)
    assert user["email"] == "renamed@test.local"
    assert user["display_name"] == "Renamed User"
    assert user["role"] == "admin"


def test_admin_delete_user_cascades_owned_data(admin_client):
    import json
    from app.database import get_db
    from app.services import (
        agent_service,
        auth_service,
        memory_service,
        workspace_service,
        credential_service,
        activity_service,
    )

    created = auth_service.create_user(
        "cleanup-user",
        "cleanup-user@test.local",
        "testpassword123",
        "Cleanup User",
    )
    workspace_service.create_workspace(
        "cleanup-workspace", "Cleanup Workspace", created["id"]
    )
    agent_service.create_agent("cleanup-agent", "Cleanup Agent", created["id"])
    agent_service.create_agent(
        "cleanup-observer",
        "Cleanup Observer",
        "admin",
        default_user_id=created["id"],
        read_scopes=[
            "agent:cleanup-observer",
            "user:cleanup-user",
            "workspace:cleanup-workspace",
            "agent:cleanup-agent",
        ],
        write_scopes=[
            "agent:cleanup-observer",
            "user:cleanup-user",
            "workspace:cleanup-workspace",
            "agent:cleanup-agent",
        ],
    )
    memory_service.write_memory("user memory", "fact", "user:cleanup-user")
    memory_service.write_memory(
        "workspace memory", "fact", "workspace:cleanup-workspace"
    )
    memory_service.write_memory("agent memory", "fact", "agent:cleanup-agent")
    credential_service.create_credential(
        "user:cleanup-user",
        "user secret",
        value_plaintext="user secret",
        created_by="admin",
    )
    credential_service.create_credential(
        "workspace:cleanup-workspace",
        "workspace secret",
        value_plaintext="workspace secret",
        created_by="admin",
    )
    credential_service.create_credential(
        "agent:cleanup-agent",
        "agent secret",
        value_plaintext="agent secret",
        created_by="admin",
    )
    activity_service.create_activity(
        "cleanup-agent", created["id"], "Cleanup task", "workspace:cleanup-workspace"
    )

    r = admin_client.delete("/api/auth/users/cleanup-user")
    assert r.status_code == 200, r.json()

    with get_db() as conn:
        assert (
            conn.execute("SELECT 1 FROM users WHERE id = 'cleanup-user'").fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM workspaces WHERE owner_user_id = 'cleanup-user'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM agents WHERE owner_user_id = 'cleanup-user'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM memory_records WHERE scope IN ('user:cleanup-user', 'workspace:cleanup-workspace', 'agent:cleanup-agent')"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM credentials WHERE scope IN ('user:cleanup-user', 'workspace:cleanup-workspace', 'agent:cleanup-agent')"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM agent_activity WHERE user_id = 'cleanup-user' OR agent_id = 'cleanup-agent'"
            ).fetchone()
            is None
        )
        observer = conn.execute(
            "SELECT default_user_id, read_scopes_json, write_scopes_json FROM agents WHERE id = 'cleanup-observer'"
        ).fetchone()
    assert observer["default_user_id"] == "admin"
    assert "cleanup-user" not in json.dumps(json.loads(observer["read_scopes_json"]))
    assert "cleanup-workspace" not in json.dumps(
        json.loads(observer["read_scopes_json"])
    )
    assert "cleanup-agent" not in json.dumps(json.loads(observer["read_scopes_json"]))
    assert "cleanup-user" not in json.dumps(json.loads(observer["write_scopes_json"]))
    assert "cleanup-workspace" not in json.dumps(
        json.loads(observer["write_scopes_json"])
    )
    assert "cleanup-agent" not in json.dumps(json.loads(observer["write_scopes_json"]))


def test_admin_can_update_system_behavior_settings(admin_client):
    r = admin_client.post(
        "/api/dashboard/system-settings",
        json={"scratchpad_retention_days": "14", "solo_mode_enabled": "false"},
    )
    assert r.status_code == 200, r.json()

    from app.database import get_db

    with get_db() as conn:
        retention = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'scratchpad_retention_days'"
        ).fetchone()
        solo = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'solo_mode_enabled'"
        ).fetchone()

    assert retention["value"] == "14"
    assert solo["value"] == "false"


def test_system_behavior_settings_validate_real_ranges(admin_client):
    r = admin_client.post(
        "/api/dashboard/system-settings",
        json={"scratchpad_retention_days": "0", "solo_mode_enabled": "maybe"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_cookie_secure_setting_controls_login_cookie(test_client, clean_db):
    from app.config import settings

    original = settings.COOKIE_SECURE
    try:
        settings.COOKIE_SECURE = True
        test_client.post(
            "/api/auth/register",
            json={
                "email": "secure@test.local",
                "password": "testpassword123",
                "display_name": "Secure User",
            },
        )
        test_client.cookies.clear()

        r = test_client.post(
            "/api/auth/login",
            json={
                "email": "secure@test.local",
                "password": "testpassword123",
            },
        )
        assert r.status_code == 200
        assert "Secure" in r.headers.get("set-cookie", "")
    finally:
        settings.COOKIE_SECURE = original


def test_settings_page_shows_timezone_picker(authenticated_client):
    html = authenticated_client.get("/settings").text
    assert "Preferences" in html
    assert 'id="user-timezone"' in html
    assert "America/New_York" in html


def test_user_settings_saves_timezone(authenticated_client):
    from app.services.auth_service import get_user_by_id

    r = authenticated_client.post(
        "/api/dashboard/user-settings",
        json={"timezone": "America/New_York"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["timezone"] == "America/New_York"
    assert get_user_by_id("testuser")["timezone"] == "America/New_York"


def test_user_settings_rejects_invalid_timezone(authenticated_client):
    r = authenticated_client.post(
        "/api/dashboard/user-settings",
        json={"timezone": "Mars/Olympus_Mons"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_TIMEZONE"


def test_user_settings_clears_timezone(authenticated_client):
    from app.services.auth_service import get_user_by_id

    authenticated_client.post(
        "/api/dashboard/user-settings", json={"timezone": "Europe/London"}
    )
    r = authenticated_client.post(
        "/api/dashboard/user-settings", json={"timezone": ""}
    )
    assert r.status_code == 200
    assert get_user_by_id("testuser")["timezone"] is None


def test_session_carries_timezone(clean_db):
    from app.services.auth_service import (
        create_user,
        create_session,
        validate_session,
        update_user_timezone,
    )

    create_user("tzuser", "tz@test.local", "testpassword123", "TZ User", "user")
    update_user_timezone("tzuser", "Asia/Tokyo")
    session = create_session("tzuser", channel="dashboard")
    validated = validate_session(session["session_id"])
    assert validated["timezone"] == "Asia/Tokyo"

