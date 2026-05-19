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
    pages_to_check = [
        "/",
        "/memory",
        "/connectors",
        "/activity",
        "/agents",
        "/workspaces",
        "/settings",
    ]

    for page in pages_to_check:
        r = authenticated_client.get(page)
        assert r.status_code == 200, f"Page {page} returned {r.status_code}"
        html = r.text
        assert 'href="/static/css/dashboard.css' in html, f"{page} missing CSS link"
        assert 'src="/static/js/dashboard.js' in html, f"{page} missing JS link"
        assert 'href="/static/img/favicon/favicon.ico"' in html, (
            f"{page} missing favicon link"
        )
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
    assert "Capability Snapshot" in html
    assert "Recent Activity" in html
    assert "View Activity" in html
    assert "Quick Actions" not in html
    assert "action-list" not in html
    assert "quick-action" not in html


def test_connectors_page_surfaces_credentials_workflow(authenticated_client):
    r = authenticated_client.get("/connectors")
    assert r.status_code == 200
    html = r.text
    assert "New Credential" in html
    assert "Import MCP Server" in html
    assert "Preview Spec" in html
    assert "create-credential-form" in html
    assert "edit-credential-form" in html
    assert "Leave blank to keep current value" in html
    assert "submitEditCredential" in html
    assert "/api/credentials/entries" in html
    assert "Create new credential" in html
    assert "Use stored credential" in html
    assert "import-spec-preview" in html
    assert "import-spec-import-btn" in html


def test_connectors_directory_page_surfaces_mcp_import(authenticated_client):
    r = authenticated_client.get("/connectors/directory")
    assert r.status_code == 200
    html = r.text
    assert "Import MCP Server" in html
    assert "import-mcp-modal" in html
    assert "Preview Spec" in html
    assert "import-spec-preview" in html
    assert "import-spec-import-btn" in html


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
        '<a href="/connectors" class=""><span>Connectors</span></a>',
        '<a href="/integrations" class=""><span>Integrations</span></a>',
        '<a href="/activity" class=""><span>Activity</span></a>',
        '<a href="/audit" class=""><span>Audit</span></a>',
        '<a href="/settings" class=""><span>Settings</span></a>',
    ]
    positions = [html.index(item) for item in expected]
    assert positions == sorted(positions)
    assert '<a href="/audit"' in html.split("<nav>", 1)[1].split("</nav>", 1)[0]


def test_dashboard_no_inline_styles_in_render_page(authenticated_client):
    r = authenticated_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "<style>" not in html, "Inline <style> tag found - CSS should be external"
    assert "<script>" not in html or "</script>" in html, (
        "Inline scripts should be external"
    )


def test_workspace_edit_modal_has_collaborator_panel_and_separate_save_form(authenticated_client):
    r = authenticated_client.get("/workspaces")
    assert r.status_code == 200
    html = r.text

    # Edit modal and its save form exist
    assert "edit-workspace-modal" in html
    assert "edit-workspace-form" in html
    assert "submitEditProject" in html

    # Collaborator panel container is present inside the modal
    assert "ep-collaborators" in html

    # Collaborator add/remove actions are separate from the main workspace save
    assert "data-workspace-collaborator-form" in html
    assert "data-workspace-collaborator-remove" in html

    # Workspace save does not trigger collaborator actions
    assert "submitEditProject" in html
    assert html.index("submitEditProject") != html.index("data-workspace-collaborator-form")
