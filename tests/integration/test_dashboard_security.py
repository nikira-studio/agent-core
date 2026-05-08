import pytest


def test_audit_page_requires_admin(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    admin_session = session["session_id"]

    r = test_client.get("/audit", cookies={"session_token": admin_session})
    assert r.status_code == 200


def test_non_admin_cannot_access_audit(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("regular", "user@test.local", "testpassword123", "Regular User", "user")
    session = create_session("regular", channel="dashboard")
    user_session = session["session_id"]

    r = test_client.get("/audit", cookies={"session_token": user_session})
    assert r.status_code == 403
    assert "Admin Access Required" in r.text
    assert "Back to Overview" in r.text
    assert 'href="/audit"' not in r.text


def test_dashboard_vault_filters_by_readable_scopes(test_client, clean_db):
    from app.services.auth_service import create_user, create_session
    from app.services.vault_service import create_vault_entry

    create_user("user1", "user1@test.local", "testpassword123", "User One", "user")
    create_user("user2", "user2@test.local", "testpassword123", "User Two", "user")
    session1 = create_session("user1", channel="dashboard")
    session2 = create_session("user2", channel="dashboard")
    user1_session = session1["session_id"]

    create_vault_entry(
        scope="user:user1",
        name="user1-secret",
        value_plaintext="user1_secret_value",
        value_type="other",
        created_by="user1",
    )

    create_vault_entry(
        scope="user:user2",
        name="user2-secret",
        value_plaintext="user2_secret_value",
        value_type="other",
        created_by="user2",
    )

    vault_html = test_client.get(
        "/connectors", cookies={"session_token": user1_session}
    ).text
    assert "user1-secret" in vault_html
    assert "user2-secret" not in vault_html


def test_dashboard_routes_importable():
    from app.routes import dashboard

    assert hasattr(dashboard, "router")
    assert hasattr(dashboard, "audit_page")
