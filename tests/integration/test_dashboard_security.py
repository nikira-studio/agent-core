

def test_audit_page_requires_admin(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    admin_session = session["session_id"]

    test_client.cookies.set("session_token", admin_session)
    r = test_client.get("/audit")
    assert r.status_code == 200


def test_non_admin_cannot_access_audit(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("regular", "user@test.local", "testpassword123", "Regular User", "user")
    session = create_session("regular", channel="dashboard")
    user_session = session["session_id"]

    test_client.cookies.set("session_token", user_session)
    r = test_client.get("/audit")
    assert r.status_code == 403
    assert "Admin Access Required" in r.text
    assert "Back to Overview" in r.text
    assert 'href="/audit"' not in r.text


def test_dashboard_connectors_filters_credentials_by_readable_scopes(
    test_client, clean_db
):
    from app.services.auth_service import create_user, create_session
    from app.services.credential_service import create_credential

    create_user("user1", "user1@test.local", "testpassword123", "User One", "user")
    create_user("user2", "user2@test.local", "testpassword123", "User Two", "user")
    session1 = create_session("user1", channel="dashboard")
    create_session("user2", channel="dashboard")
    user1_session = session1["session_id"]

    create_credential(
        scope="user:user1",
        name="user1-secret",
        value_plaintext="user1_secret_value",
        created_by="user1",
    )

    create_credential(
        scope="user:user2",
        name="user2-secret",
        value_plaintext="user2_secret_value",
        created_by="user2",
    )

    test_client.cookies.set("session_token", user1_session)
    connectors_html = test_client.get("/connectors").text
    assert "user1-secret" in connectors_html
    assert "user2-secret" not in connectors_html


def test_dashboard_routes_importable():
    from app.routes import dashboard

    assert hasattr(dashboard, "router")
    assert hasattr(dashboard, "audit_page")
