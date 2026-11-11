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
    # The count tiles are gone: each count lives on its own page, and Health says how things are.
    assert 'class="stat-card' not in html.split("overview-health", 1)[0].split("overview-waiting", 1)[0]
    for label in ("Active Agents", "Active Workspaces", "Open tasks"):
        assert f'<div class="label">{label}</div>' not in html
    assert "Waiting on you" in html
    assert "Capability Snapshot" not in html
    assert "Health" in html
    assert "Recent Activity" in html
    assert "View Activity" in html
    assert "Quick Actions" not in html
    assert "action-list" not in html
    assert "quick-action" not in html
    # localDt returns a span. The live event refresh must insert that markup,
    # not escape it as visible text, and then localize the new timestamp.
    assert "'<td class=\"hide-phone\">' + ts + '</td>'" in html
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
    for label in ("Agent Core", "Services", "Webhooks", "Local credential helper"):
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
    assert "create a new one only for a separate tool" in agents
    assert "Does an existing agent already do this job?" in agents
    workspaces = authenticated_client.get("/workspaces").text
    assert "A workspace is one project's shared memory" in workspaces
    assert "Create one per project or app." in workspaces
    assert "Does an existing workspace already fit?" not in workspaces


def test_static_links_carry_the_files_own_version_so_a_changed_stylesheet_reaches_the_browser(authenticated_client):
    import hashlib
    import re
    from pathlib import Path

    html = authenticated_client.get("/").text
    css = re.search(r'/static/css/dashboard\.css\?v=([0-9a-f]+)', html)
    assert css, "the stylesheet link must carry a content version"
    expected = hashlib.sha256(Path("app/dashboard/static/css/dashboard.css").read_bytes()).hexdigest()[:10]
    assert css.group(1) == expected
    assert "20260828" not in html  # no hand-set version left behind


def test_credentials_rows_have_no_wide_reference_column_and_offer_a_copy_button(authenticated_client):
    from app.services import credential_service

    entry = credential_service.create_credential("user:admin", "wide-name-" + "x" * 40, "not-shown")
    html = authenticated_client.get("/credentials").text
    assert "<th>Reference</th>" not in html
    assert f'data-copy-reference="{entry["reference_name"]}"' in html
    assert "stack-table" in html  # one block per credential on a phone


def test_text_that_looks_like_an_asset_link_cannot_break_a_page(authenticated_client):
    from app.routes.dashboard_shared import _versioned_assets
    from app.services import credential_service

    # Not a file, a directory, a missing file, and prose: none may raise or be rewritten.
    for text in ("/static/css/..?v=1", "/static/js/.?v=1", "/static/css/nope.css?v=1", "see /static/css/dashboard.css?v=1 here"):
        assert _versioned_assets(text) == text
    credential_service.create_credential("user:admin", "see /static/css/..?v=1 and /static/js/..?v=2", "not-shown")
    page = authenticated_client.get("/credentials")
    assert page.status_code == 200 and "/static/css/..?v=1" in page.text


def test_credential_row_buttons_share_a_line_and_scopes_break_only_at_the_colon(authenticated_client):
    from app.services import credential_service

    credential_service.create_credential("workspace:home-network", "a_long_credential_name", "not-shown")
    html = authenticated_client.get("/credentials").text
    assert "<span class='scope-name'>home-network</span> <span class='scope-kind' title='workspace:home-network'>workspace</span>" in html
    assert "a_<wbr>long_<wbr>credential_<wbr>name" in html
    css = open("app/dashboard/static/css/dashboard.css").read()
    assert ".actions-cell .btn { vertical-align: middle; }" in css  # text and icon buttons on one baseline


def test_wording_batch_one_fixes_the_text_that_did_not_match_the_screen(authenticated_client):
    settings = authenticated_client.get("/settings").text
    assert "{CREDENTIAL_PREFIX}" not in settings and "AC_SECRET_*" in settings
    # The maintenance help sits with its own button, not beside Export Encrypted Backup.
    backup = settings.split("<h2>Backup & Restore</h2>", 1)[1]
    export_part, maintenance_part = backup.split("<h3 class=\"sub-heading section-title\">Maintenance</h3>", 1)
    assert "Maintenance marks tasks stale" not in export_part
    assert "Run Maintenance" in maintenance_part and "Maintenance marks tasks stale" in maintenance_part
    assert "STALE_THRESHOLD" not in settings

    # Row buttons only render when a task exists, so check the template itself.
    source = Path("app/routes/activity_page.py").read_text()
    assert "title='Start'>Start" in source and "title='Reactivate'" not in source and "title='Complete'" not in source

    webhooks = authenticated_client.get("/webhooks").text
    assert "Outgoing webhooks only notify" in webhooks

    users = authenticated_client.get("/users").text
    create, edit = users.split('id="edit-user-modal"', 1)
    assert 'id="eu-active"' not in create and 'id="eu-active"' in edit  # one checkbox, in the dialog that uses it
    assert "Admin role grants full dashboard access" not in users


def test_row_actions_are_plain_buttons_and_the_generate_button_is_the_blue_main_action(authenticated_client):
    for path in ("/agents", "/workspaces", "/memory"):
        assert "btn-warning" not in authenticated_client.get(path).text, path


def test_delete_asks_for_the_name_and_says_what_goes_with_it(authenticated_client):
    workspaces = authenticated_client.get("/workspaces").text
    assert "counts.memories" in workspaces and "Type the workspace ID to confirm" in workspaces and "kept but no longer belong" in workspaces
    agents = authenticated_client.get("/agents").text
    assert "Type the agent ID to confirm" in agents and "its private memory" in agents


def test_activity_page_puts_tasks_first_and_synchronization_last(authenticated_client):
    html = authenticated_client.get("/activity").text
    assert html.index("<h2>Tasks</h2>") < html.index("Workspace synchronization")
    # The snapshot counters and coverage percentages were removed: Waiting on you and the task list answer it.
    assert "Coordination Snapshot" not in html and "Execution-linked" not in html
    # Empty synchronization is closed; assigning a task is still on this page.
    assert '<details class="card card-fold" id="workspace-sync" >' in html
    assert "+ Assign a task" in html


def test_write_access_chips_are_not_the_danger_colour():
    css = Path("app/dashboard/static/css/dashboard.css").read_text()
    block = css.split(".scope-tag.scope-write {", 1)[1].split("}", 1)[0]
    assert "danger" not in block


def test_workspaces_table_counts_memories(authenticated_client):
    assert "<th>Memories</th>" in authenticated_client.get("/workspaces").text


def test_wording_batch_two(authenticated_client):
    settings = authenticated_client.get("/settings").text
    assert "vault" not in settings.lower()
    assert "Search by meaning (vector search)" in settings and "Enable search by meaning" in settings
    assert "Keyring size" not in settings
    assert "Rotate Key</strong>: makes a new key" in settings
    # The Audit Log shortcut no longer sits in the Backup & Restore header.
    assert '<a href="/audit" class="btn btn-sm btn-secondary">Audit Log</a>' not in settings
    assert "Checks that the data folder is writable" in settings

    credentials = authenticated_client.get("/credentials").text
    assert "vault" not in credentials.lower()

    audit = authenticated_client.get("/audit").text
    assert "With Details" not in audit
    approvals = authenticated_client.get("/approvals").text
    assert "<th>Decided</th>" in approvals and "<th>Scope</th>" not in approvals


def test_memory_list_is_paged_and_can_be_filtered_by_workspace(authenticated_client):
    from app.services import memory_service

    for i in range(30):
        memory_service.write_memory(content=f"alpha memory number {i}", memory_class="fact", scope="workspace:alpha", topic=f"alpha-{i}")
    for i in range(3):
        memory_service.write_memory(content=f"beta memory number {i}", memory_class="fact", scope="workspace:beta", topic=f"beta-{i}")
    first = authenticated_client.get("/memory").text
    table = first.split('id="mem-results-body"', 1)[1].split("</tbody>", 1)[0]
    assert table.count("<tr data-scope=") == 25
    assert "Page 1 of 2" in first and "Showing 1–25 of 33" in first and "?page=2" in first
    second = authenticated_client.get("/memory?page=2").text
    assert second.split('id="mem-results-body"', 1)[1].split("</tbody>", 1)[0].count("<tr data-scope=") == 8
    beta = authenticated_client.get("/memory?workspace=workspace:beta").text
    rows = beta.split('id="mem-results-body"', 1)[1].split("</tbody>", 1)[0]
    assert rows.count("<tr data-scope=") == 3 and "alpha memory" not in rows
    assert 'class="stack-cards"' in first and "mem-content" in first
    assert authenticated_client.get("/memory?page=999").status_code == 200  # clamps instead of failing


def test_new_binding_dialog_shows_one_credential_choice_at_a_time(authenticated_client):
    html = authenticated_client.get("/connectors").text
    assert 'id="binding-existing-credential-fields" class="form-group" style="display:none"' in html
    assert "Who may use it" in html and "A binding lets agents in one workspace" in html
    assert "<label>Scope *</label>" not in html and "Just me (personal)" in html


def test_agent_rows_keep_edit_and_connect_and_put_the_rest_under_more(authenticated_client, test_client):
    from app.services import agent_service

    agent_service.create_agent("rowtest", "Row Test", "admin", capabilities=["memory"])
    html = authenticated_client.get("/agents").text
    row = html.split("data-agent-edit='rowtest'", 1)[1].split("</tr>", 1)[0]
    assert "Connect</a>" in row and "<summary" in row and "More</summary>" in row
    before_more, after_more = row.split("More</summary>", 1)
    assert "data-agent-purge" not in before_more and "data-agent-deactivate" not in before_more
    assert "data-agent-purge='rowtest'" in after_more and "data-agent-deactivate='rowtest'" in after_more


def test_the_two_key_rotations_say_what_keeps_working_and_what_stops_before_they_run(authenticated_client):
    html = authenticated_client.get("/settings").text
    assert "confirm('Rotate broker" not in html
    assert 'id="broker-rotate-modal"' in html and "Stops working:" in html and "Keeps working:" in html
    assert "Changes now:" in html
    assert 'class="btn btn-warning" onclick="openModal(\'broker-rotate-modal\')"' in html
    assert 'btn btn-danger" onclick="openModal(\'credential-key-rotate-modal\')"' not in html


def test_phone_navigation_keeps_its_group_headings():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "app/dashboard/static/css/dashboard.css").read_text()
    assert ".sidebar nav .nav-heading { display: none; }" not in css
    assert "flex: 0 0 100%" in css.split("The group headings stay on a phone", 1)[1][:400]


def test_every_dashboard_page_has_headings_in_order(authenticated_client):
    """Page title h1, card titles h2, anything inside a card h3: no page jumps a level."""
    import re

    pages = ["/", "/activity", "/approvals", "/memory", "/users", "/agents", "/workspaces", "/integrations",
             "/connectors", "/connectors/directory", "/connectors/adapters", "/connectors/help", "/credentials",
             "/audit", "/webhooks", "/settings"]
    for path in pages:
        html = authenticated_client.get(path).text
        body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
        levels = [int(m) for m in re.findall(r"<h([1-4])[\s>]", body)]
        assert levels and levels[0] == 1, (path, levels[:5])
        previous = 0
        for level in levels:
            assert level <= previous + 1, (path, "jumps from h%d to h%d" % (previous, level))
            previous = level


def test_overview_waiting_list_shows_every_kind_to_an_admin(authenticated_client):
    from app.database import get_db
    from app.services import activity_service, credential_request_service

    activity = activity_service.create_activity("admin", "admin", "Needs a decision")
    activity_service.set_handoff(activity["id"], {"needs_human": "Pick the vendor"})
    credential_request_service.request_credential(
        agent_id="admin", user_id=None, scope="user:admin", name="VENDOR_TOKEN", purpose="x"
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_proposals (id, rule, action, scope, target_ids_json, rationale, evidence_json, status, created_at) "
            "VALUES ('sug-ov', 'unconfirmed_inference', 'confirm', 'user:admin', '[\"m-ov\"]', 'r', '{}', 'pending', '2026-11-11T00:00:00+00:00')"
        )
        conn.commit()

    html = authenticated_client.get("/").text
    waiting = html.split('id="overview-waiting"', 1)[1].split("</table>", 1)[0]
    assert "Pick the vendor" in waiting
    assert "needs VENDOR_TOKEN" in waiting
    assert "1 suggestion to review" in waiting
