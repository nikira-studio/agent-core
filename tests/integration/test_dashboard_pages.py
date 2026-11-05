from pathlib import Path

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
        assert 'src="/static/js/events.js' in html, f"{page} missing event stream JS"
        assert "window.AC_AUTHENTICATED = true" in html
        assert 'href="/static/img/favicon/favicon.ico"' in html, (
            f"{page} missing favicon link"
        )
        assert 'src="/static/img/logo.png"' in html, f"{page} missing logo"


def test_connectors_page_renders_execution_without_failure_category(
    authenticated_client,
):
    from app.services import connector_service

    connector_type = connector_service.list_connector_types()[0]
    binding = connector_service.create_binding(
        connector_type_id=connector_type["id"],
        name="Null category binding",
        scope="user:admin",
        created_by="admin",
    )
    connector_service.log_execution(
        binding_id=binding["id"],
        action="test",
        params_json="{}",
        result_status="failure",
        error_message="A useful error message",
    )

    r = authenticated_client.get("/connectors")
    assert r.status_code == 200
    assert "A useful error message" in r.text


def test_credentials_page_allows_a_credential_scope_move(authenticated_client):
    r = authenticated_client.get("/credentials")
    assert r.status_code == 200
    assert '<select id="edit-credential-scope" required>' in r.text
    assert "Moving a credential changes who can access the stored secret" in r.text


def test_public_auth_pages_do_not_start_the_authenticated_event_stream(test_client):
    for page in ("/login", "/otp"):
        r = test_client.get(page)
        assert r.status_code == 200
        assert 'src="/static/js/events.js' not in r.text
        assert "window.AC_AUTHENTICATED = false" in r.text

    dashboard_js = Path("app/dashboard/static/js/dashboard.js").read_text()
    assert "window.AC_AUTHENTICATED && typeof apiFetch" in dashboard_js


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
    assert "Waiting on you" in html
    assert "Memory Records" in html
    assert "Capability Snapshot" not in html
    assert "Health" in html
    assert "Recent Activity" in html
    assert "View Activity" in html
    assert "Quick Actions" not in html
    assert "action-list" not in html
    assert "quick-action" not in html
    # localDt returns a span. The live event refresh must insert that markup,
    # not escape it as visible text, and then localize the new timestamp.
    assert "'<td>' + ts + '</td>'" in html
    assert "applyLocalTimes(tbody);" in html


def test_overview_attention_omits_resolved_and_historical_activities(authenticated_client):
    from app.database import get_db
    from app.services import activity_service

    old = activity_service.create_activity("admin", "admin", "Old blocked task")
    activity_service.update_activity(old["id"], status="blocked")
    resolved = activity_service.create_activity("admin", "admin", "Resolved retry")
    activity_service.update_activity(resolved["id"], status="blocked")
    retry = activity_service.create_activity("admin", "admin", "Resolved retry")
    activity_service.update_activity(retry["id"], status="completed")
    current = activity_service.create_activity("admin", "admin", "Current blocker")
    activity_service.update_activity(current["id"], status="blocked")
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_activity SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (old["id"],),
        )
        conn.commit()

    html = authenticated_client.get("/").text
    attention = html.split('id="overview-waiting"', 1)[1].split("</table>", 1)[0]
    assert "Current blocker" in attention
    assert "Old blocked task" not in attention
    assert "Resolved retry" not in attention


def test_admin_activity_summary_keeps_global_overview_scope(authenticated_client):
    from app.services import activity_service

    activity = activity_service.create_activity(
        "another-agent", "another-user", "Visible to the admin live refresh"
    )
    response = authenticated_client.get("/api/dashboard/activity/summary")
    assert response.status_code == 200
    data = response.json()["data"]
    assert any(item["id"] == activity["id"] for item in data["recent"])


def test_connectors_page_surfaces_credentials_workflow(authenticated_client):
    r = authenticated_client.get("/connectors")
    assert r.status_code == 200
    html = r.text
    # Connectors links out to credential management and offers inline
    # credential selection on the binding form; full credential CRUD lives
    # on /credentials (see test_credentials_page_surfaces_credential_forms).
    assert "Create new credential" in html
    assert "Import MCP Server" in html
    assert "Preview Spec" in html
    # JS wiring (incl. /api/credentials/entries) now lives in the static bundle.
    assert "/static/js/connectors.js" in html
    assert "Create new credential" in html
    assert "Use stored credential" in html
    assert '<label for="binding-credential">Stored Credential</label>' in html
    assert "oauth-redirect-modal" in html
    assert "Copy URL" in html
    assert "import-spec-preview" in html
    assert "import-spec-import-btn" in html
    assert "Setup Instructions" in html


def test_binding_guidance_includes_adapter_setup_metadata():
    from app.routes.connectors_page import _binding_guidance_for_connector_type

    guidance = _binding_guidance_for_connector_type(
        {
            "id": "example",
            "display_name": "Example",
            "required_credential_fields": ["token"],
        },
        {
            "setup": {
                "instructions": "Create a token in the provider console.",
                "documentation_url": "https://example.com/setup",
            }
        },
    )
    assert guidance["setup_instructions"] == "Create a token in the provider console."
    assert guidance["documentation_url"] == "https://example.com/setup"


def test_credentials_page_surfaces_credential_forms(authenticated_client):
    r = authenticated_client.get("/credentials")
    assert r.status_code == 200
    html = r.text
    assert "create-credential-form" in html
    assert "edit-credential-form" in html
    assert "Leave blank to keep current value" in html
    assert "submitEditCredential" in html
    # API wiring lives in the externalized static JS now.
    assert "/static/js/credentials.js" in html


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
        '<a href="/" class="active" aria-current="page"><span>Overview</span></a>',
        '<a href="/activity" class=""><span>Activity</span></a>',
        '<a href="/approvals" class=""><span>Approvals</span></a>',
        '<a href="/memory" class=""><span>Memory</span></a>',
        '<a href="/users" class=""><span>Users</span></a>',
        '<a href="/agents" class=""><span>Agents</span></a>',
        '<a href="/workspaces" class=""><span>Workspaces</span></a>',
        '<a href="/integrations" class=""><span>Connect</span></a>',
        '<a href="/connectors" class=""><span>Services</span></a>',
        '<a href="/credentials" class=""><span>Credentials</span></a>',
        '<a href="/audit" class=""><span>Audit</span></a>',
        '<a href="/webhooks" class=""><span>Webhooks</span></a>',
        '<a href="/settings" class=""><span>Settings</span></a>',
    ]
    positions = [html.index(item) for item in expected]
    assert positions == sorted(positions)
    assert '<a href="/audit"' in html.split("<nav>", 1)[1].split("</nav>", 1)[0]


def test_dashboard_shared_accessibility_behaviour(authenticated_client):
    html = authenticated_client.get("/users").text
    dashboard_js = Path("app/dashboard/static/js/dashboard.js").read_text()

    assert 'aria-current="page"' in html
    assert "toast.setAttribute('role'" in dashboard_js
    assert "toast.setAttribute('aria-atomic', 'true')" in dashboard_js
    assert "dialog.setAttribute('role', 'dialog')" in dashboard_js
    assert "dialog.setAttribute('aria-modal', 'true')" in dashboard_js
    assert "visibleFocusableElements(activeModalOverlay)" in dashboard_js
    assert "label.htmlFor = controlId" in dashboard_js
    assert "control.setAttribute('aria-labelledby'" in dashboard_js
    assert "enhanceDashboardAccessibility(document)" in dashboard_js


def test_otp_input_has_a_visible_programmatic_label(test_client):
    html = test_client.get("/otp").text

    assert '<label for="otp-code">Authentication code</label>' in html
    assert 'id="otp-code"' in html
    assert 'inputmode="numeric"' in html


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


def test_overview_says_so_when_nothing_is_waiting_and_shows_health(authenticated_client):
    html = authenticated_client.get("/").text
    assert "Nothing is waiting on you." in html
    health = html.split('id="overview-health"', 1)[1]
    for label in ("Agent Core", "Services", "Webhooks", "Broker credential"):
        assert label in health


def test_overview_health_counts_a_failing_binding_and_a_dead_webhook(authenticated_client):
    from app.database import get_db
    from app.routes.overview_page import _health_items

    with get_db() as conn:
        conn.execute(
            "INSERT INTO webhook_delivery_log (webhook_id, event_id, event_type, payload_json, status, last_attempt_at) "
            "VALUES ('w', 'e', 'x', '{}', 'dead', datetime('now'))"
        )
        conn.commit()
    items = {label: (value, ok) for label, value, _href, ok in _health_items(True)}
    assert items["Webhooks"] == ("1 failed in 24 hours", False)


def test_a_fresh_install_shows_getting_started(authenticated_client):
    html = authenticated_client.get("/").text
    assert 'id="overview-getting-started"' in html
    assert "Add an agent" in html and "Connect its tool" in html


def test_services_page_has_one_add_entry_and_explains_itself(authenticated_client):
    html = authenticated_client.get("/connectors").text
    assert html.count("Add a service") >= 2  # the button, and the quick-start step
    header = html.split('class="page-actions"', 1)[1].split("stat-grid", 1)[0]
    for way in ("From an API spec", "From an MCP server", "A plain web service"):
        assert way in header
    assert "+ New Credential" not in header and "Browse Adapters" not in header
    assert "No bindings yet." in html  # built-in services exist, bindings do not
    assert "Getting a service working" in html  # nothing is bound yet


def test_services_page_tucks_expert_fields_under_advanced(authenticated_client):
    html = authenticated_client.get("/connectors").text
    create = html.split('id="create-binding-modal"', 1)[1].split('id="edit-binding-modal"', 1)[0]
    advanced = create.split("<summary>Advanced</summary>", 1)[1].split("</details>", 1)[0]
    assert 'id="binding-config"' in advanced and 'id="binding-mcp-endpoint"' in advanced
    edit = html.split('id="edit-binding-modal"', 1)[1].split('id="import-spec-modal"', 1)[0]
    advanced = edit.split("<summary>Advanced</summary>", 1)[1].split("</details>", 1)[0]
    assert 'id="edit-binding-config"' in advanced and 'id="edit-binding-mcp-endpoint"' in advanced


def test_services_page_run_history_is_collapsed(authenticated_client):
    html = authenticated_client.get("/connectors").text
    assert '<details class="card" id="executions">' in html
    assert "<details class=\"card\" id=\"executions\" open" not in html


def _hint_follows(html, element_id):
    """True when the control with this id is followed by a help line before the next control."""
    after = html.split(f'id="{element_id}"', 1)[1]
    nxt = min((i for i in (after.find("<input"), after.find("<select"), after.find("<textarea"), after.find("<button")) if i != -1), default=len(after))
    return "form-hint" in after[:nxt]


def test_every_field_on_the_reworked_forms_has_a_line_of_help(authenticated_client):
    expected = {
        "/connectors": [
            "import-spec-url", "import-spec-json", "import-spec-name", "import-mcp-url", "import-mcp-name",
            "import-mcp-description", "import-mcp-transport", "import-mcp-timeout", "http-display-name",
            "http-base-url", "http-auth-type", "http-extra-headers", "binding-name", "binding-credential",
            "binding-config", "edit-binding-name", "edit-binding-scope", "edit-binding-credential",
            "edit-binding-config",
        ],
        "/agents": ["ca-display-name", "ca-description", "edit-display-name", "edit-description"],
        "/settings": ["op-token-label", "op-token-minutes", "vector-provider", "vector-auth-type", "prune-activity-before"],
        "/workspaces": ["cp-name", "cp-description", "ep-name", "ep-description"],
        "/users": ["eu-display-name", "eu-email", "eu-role", "eu-password"],
    }
    missing = [
        f"{path} {element_id}"
        for path, ids in expected.items()
        for element_id in ids
        if not _hint_follows(authenticated_client.get(path).text, element_id)
    ]
    assert not missing, missing


def test_agents_and_workspaces_pages_prefer_extending_what_exists(authenticated_client):
    agents = authenticated_client.get("/agents").text
    assert "Create a new agent only for a separate tool or identity" in agents
    assert "Does an existing agent already do this job?" in agents
    workspaces = authenticated_client.get("/workspaces").text
    assert "is the normal way to share" in workspaces
    assert "Does an existing workspace already fit?" in workspaces
