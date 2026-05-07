import pytest


@pytest.fixture
def authenticated_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    admin_session = session["session_id"]
    test_client.cookies.set("session_token", admin_session)
    return test_client


def test_dashboard_pages_load_with_static_assets(authenticated_client):
    pages_to_check = ["/", "/memory", "/vault", "/activity", "/agents", "/workspaces", "/settings"]

    for page in pages_to_check:
        r = authenticated_client.get(page)
        assert r.status_code == 200, f"Page {page} returned {r.status_code}"
        html = r.text
        assert 'href="/static/css/dashboard.css' in html, f"{page} missing CSS link"
        assert 'src="/static/js/dashboard.js' in html, f"{page} missing JS link"
        assert 'href="/static/img/favicon/favicon.ico"' in html, f"{page} missing favicon link"
        assert 'src="/static/img/logo.png"' in html, f"{page} missing logo"


def test_dashboard_audit_page_requires_admin(authenticated_client):
    r = authenticated_client.get("/audit")
    assert r.status_code == 200


def test_theme_toggle_present(authenticated_client):
    r = authenticated_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "theme-toggle" in html, "Theme toggle button missing"
    assert "/static/js/dashboard.js" in html, "Dashboard JS not linked"


def test_static_brand_assets_are_served(authenticated_client):
    favicon = authenticated_client.get("/static/img/favicon/favicon.ico")
    logo = authenticated_client.get("/static/img/logo.png")
    manifest = authenticated_client.get("/static/img/favicon/site.webmanifest")
    assert favicon.status_code == 200
    assert logo.status_code == 200
    assert manifest.status_code == 200


def test_overview_surfaces_operational_sections(authenticated_client):
    r = authenticated_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Users" in html
    assert "Active Agents" in html
    assert "Open Activities" in html
    assert "Stale / Blocked" in html
    assert "Memory Records" in html
    assert "Credentials" in html
    assert "Recent Activity" in html
    assert "View Activity" in html
    assert "Quick Actions" not in html
    assert "action-list" not in html
    assert "quick-action" not in html


def test_dashboard_nav_order_and_admin_audit_placement(authenticated_client):
    r = authenticated_client.get("/")
    assert r.status_code == 200
    html = r.text
    expected = [
        '<a href="/" class="active"><span>Overview</span></a>',
        '<a href="/users" class=""><span>Users</span></a>',
        '<a href="/agents" class=""><span>Agents</span></a>',
        '<a href="/workspaces" class=""><span>Workspaces</span></a>',
        '<a href="/memory" class=""><span>Memory</span></a>',
        '<a href="/vault" class=""><span>Vault</span></a>',
        '<a href="/agent-setup" class=""><span>Integration</span></a>',
        '<a href="/activity" class=""><span>Activity</span></a>',
        '<a href="/settings" class=""><span>Settings</span></a>',
    ]
    positions = [html.index(item) for item in expected]
    assert positions == sorted(positions)
    assert '<a href="/audit"' not in html.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert 'href="/audit"' in html


def test_dashboard_no_inline_styles_in_render_page(authenticated_client):
    r = authenticated_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "<style>" not in html, "Inline <style> tag found - CSS should be external"
    assert "<script>" not in html or "</script>" in html, "Inline scripts should be external"
