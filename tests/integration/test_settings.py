import pytest


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
    assert "Backup Codes" in html


def test_non_admin_settings_hides_admin_controls(authenticated_client):
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Rotate Broker Credential" not in html
    assert "Backup & Restore" not in html
    assert "Vault Key" not in html


def test_admin_settings_exposes_vault_key_and_restore_mode_controls(admin_client):
    r = admin_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "Rotate Vault Key" in html
    assert "Restore Key" in html
    assert 'id="restore-mode"' in html
    assert 'value="merge"' in html
    assert "/api/vault/rotate" in html
    assert "/api/vault/restore-key" in html
    assert 'id="backup-otp" placeholder="123456 or backup code"' in html
    assert 'id="restore-otp" placeholder="123456 or backup code"' in html
    assert 'id="vault-key-rotate-otp" placeholder="123456 or backup code"' in html
    assert 'id="vault-key-restore-otp" placeholder="123456 or backup code"' in html
    assert 'id="backup-otp" placeholder="123456" maxlength="6"' not in html


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


def test_login_otp_page_accepts_backup_code_input(test_client, clean_db):
    r = test_client.get("/otp")
    assert r.status_code == 200
    html = r.text
    assert "backup code" in html
    assert "autocomplete=\"one-time-code\"" in html
    assert 'name="otp_code"' in html
    assert 'name="otp_code" placeholder="123456" maxlength="6"' not in html


def test_verify_otp_or_backup_code_consumes_backup_code(authenticated_client):
    import pyotp
    from app.services.auth_service import (
        enroll_otp,
        confirm_otp_enrollment,
        verify_otp_or_backup_code,
    )

    pending = enroll_otp("testuser")
    backup_codes = confirm_otp_enrollment("testuser", pyotp.TOTP(pending["secret"]).now())
    backup_code = backup_codes[0]

    assert verify_otp_or_backup_code("testuser", backup_code) is True
    assert verify_otp_or_backup_code("testuser", backup_code) is False


def test_settings_backup_codes_page_loads(authenticated_client):
    r = authenticated_client.get("/settings/backup-codes")
    assert r.status_code == 200
    html = r.text
    assert "Backup Codes" in html or "Recovery" in html


def test_settings_password_requires_auth(test_client, clean_db):
    r = test_client.get("/settings/password", follow_redirects=False)
    assert r.status_code == 302


def test_settings_otp_requires_auth(test_client, clean_db):
    r = test_client.get("/settings/otp", follow_redirects=False)
    assert r.status_code == 302


def test_settings_backup_codes_requires_auth(test_client, clean_db):
    r = test_client.get("/settings/backup-codes", follow_redirects=False)
    assert r.status_code == 302


def test_first_run_creates_admin_account(test_client, clean_db):
    r = test_client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert "Welcome to Agent Core" in html or "Create your admin account" in html or "Sign In" in html


def test_first_run_browser_path_reaches_otp_and_dashboard(test_client, clean_db):
    import pyotp

    r = test_client.post("/api/auth/register", json={
        "email": "admin@test.local",
        "password": "testpassword123",
        "display_name": "Admin Test",
    })
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
    assert "backup_codes" not in data

    confirm = test_client.post(
        "/api/auth/otp/confirm",
        json={"otp_code": pyotp.TOTP(data["secret"]).now()},
    )
    assert confirm.status_code == 200, confirm.json()
    assert len(confirm.json()["data"]["backup_codes"]) == 10

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

    bad = authenticated_client.post("/api/auth/otp/confirm", json={"otp_code": "000000"})
    assert bad.status_code == 401
    assert is_otp_enrolled("testuser") is False

    good = authenticated_client.post(
        "/api/auth/otp/confirm",
        json={"otp_code": pyotp.TOTP(data["secret"]).now()},
    )
    assert good.status_code == 200, good.json()
    assert is_otp_enrolled("testuser") is True
    assert len(good.json()["data"]["backup_codes"]) == 10


def test_otp_reset_keeps_existing_otp_active_until_confirmation(authenticated_client):
    import pyotp
    from app.services.auth_service import enroll_otp, confirm_otp_enrollment, is_otp_enrolled, verify_otp

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
    assert "otp-reset-result" in page.text
    assert "startOtpEnrollment('otp-reset-result')" in page.text


def test_change_password_endpoint(authenticated_client):
    r = authenticated_client.post(
        "/api/auth/password",
        json={"current_password": "testpassword123", "new_password": "newpassword456"}
    )
    assert r.status_code == 200
    assert r.json()["ok"] == True


def test_change_password_wrong_current_fails(authenticated_client):
    r = authenticated_client.post(
        "/api/auth/password",
        json={"current_password": "wrongpassword", "new_password": "newpassword456"}
    )
    assert r.status_code == 400
    assert r.json()["ok"] == False


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
        vault_service,
        activity_service,
    )

    created = auth_service.create_user(
        "cleanup-user",
        "cleanup-user@test.local",
        "testpassword123",
        "Cleanup User",
    )
    workspace_service.create_workspace("cleanup-workspace", "Cleanup Workspace", created["id"])
    agent_service.create_agent("cleanup-agent", "Cleanup Agent", created["id"])
    agent_service.create_agent(
        "cleanup-observer",
        "Cleanup Observer",
        "admin",
        default_user_id=created["id"],
        read_scopes=["agent:cleanup-observer", "user:cleanup-user", "workspace:cleanup-workspace", "agent:cleanup-agent"],
        write_scopes=["agent:cleanup-observer", "user:cleanup-user", "workspace:cleanup-workspace", "agent:cleanup-agent"],
    )
    memory_service.write_memory("user memory", "fact", "user:cleanup-user")
    memory_service.write_memory("workspace memory", "fact", "workspace:cleanup-workspace")
    memory_service.write_memory("agent memory", "fact", "agent:cleanup-agent")
    vault_service.create_vault_entry("user:cleanup-user", "user secret", "secret")
    vault_service.create_vault_entry("workspace:cleanup-workspace", "workspace secret", "secret")
    vault_service.create_vault_entry("agent:cleanup-agent", "agent secret", "secret")
    activity_service.create_activity("cleanup-agent", created["id"], "Cleanup task", "workspace:cleanup-workspace")

    r = admin_client.delete("/api/auth/users/cleanup-user")
    assert r.status_code == 200, r.json()

    with get_db() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = 'cleanup-user'").fetchone() is None
        assert conn.execute("SELECT 1 FROM workspaces WHERE owner_user_id = 'cleanup-user'").fetchone() is None
        assert conn.execute("SELECT 1 FROM agents WHERE owner_user_id = 'cleanup-user'").fetchone() is None
        assert conn.execute("SELECT 1 FROM memory_records WHERE scope IN ('user:cleanup-user', 'workspace:cleanup-workspace', 'agent:cleanup-agent')").fetchone() is None
        assert conn.execute("SELECT 1 FROM vault_entries WHERE scope IN ('user:cleanup-user', 'workspace:cleanup-workspace', 'agent:cleanup-agent')").fetchone() is None
        assert conn.execute("SELECT 1 FROM agent_activity WHERE user_id = 'cleanup-user' OR agent_id = 'cleanup-agent'").fetchone() is None
        observer = conn.execute(
            "SELECT default_user_id, read_scopes_json, write_scopes_json FROM agents WHERE id = 'cleanup-observer'"
        ).fetchone()
    assert observer["default_user_id"] == "admin"
    assert "cleanup-user" not in json.dumps(json.loads(observer["read_scopes_json"]))
    assert "cleanup-workspace" not in json.dumps(json.loads(observer["read_scopes_json"]))
    assert "cleanup-agent" not in json.dumps(json.loads(observer["read_scopes_json"]))
    assert "cleanup-user" not in json.dumps(json.loads(observer["write_scopes_json"]))
    assert "cleanup-workspace" not in json.dumps(json.loads(observer["write_scopes_json"]))
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
    assert r.json()["ok"] == False


def test_cookie_secure_setting_controls_login_cookie(test_client, clean_db):
    from app.config import settings

    original = settings.COOKIE_SECURE
    try:
        settings.COOKIE_SECURE = True
        test_client.post("/api/auth/register", json={
            "email": "secure@test.local",
            "password": "testpassword123",
            "display_name": "Secure User",
        })
        test_client.cookies.clear()

        r = test_client.post("/api/auth/login", json={
            "email": "secure@test.local",
            "password": "testpassword123",
        })
        assert r.status_code == 200
        assert "Secure" in r.headers.get("set-cookie", "")
    finally:
        settings.COOKIE_SECURE = original


def test_backup_codes_page_does_not_show_stored_plaintext(authenticated_client):
    import pyotp
    from app.services.auth_service import enroll_otp
    from app.services.auth_service import confirm_otp_enrollment

    otp = enroll_otp("testuser")
    confirm_otp_enrollment("testuser", pyotp.TOTP(otp["secret"]).now())
    r = authenticated_client.get("/settings/backup-codes")
    assert r.status_code == 200
    html = r.text
    assert "cannot be viewed again" in html
    assert "Your current backup codes" not in html
