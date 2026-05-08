import pytest


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


@pytest.fixture
def user_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("user1", "user1@test.local", "testpassword123", "User One", "user")
    session = create_session("user1", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


class TestMemoryPage:
    def test_memory_page_loads(self, admin_client):
        r = admin_client.get("/memory")
        assert r.status_code == 200

    def test_memory_detail_modal_present(self, admin_client):
        r = admin_client.get("/memory")
        assert 'id="memory-detail-modal"' in r.text
        assert "viewMemory" in r.text

    def test_memory_write_modal_present(self, admin_client):
        r = admin_client.get("/memory")
        assert 'id="write-memory-modal"' in r.text

    def test_memory_detail_button_in_table(self, admin_client):
        r = admin_client.get("/memory")
        assert "Detail" in r.text

    def test_memory_supersession_chain_section(self, admin_client):
        r = admin_client.get("/memory")
        assert "mem-detail-chain" in r.text
        assert "showChain" in r.text


class TestAgentsPage:
    def test_agents_page_loads(self, admin_client):
        r = admin_client.get("/agents")
        assert r.status_code == 200

    def test_agents_edit_modal_present(self, admin_client):
        r = admin_client.get("/agents")
        assert 'id="edit-agent-modal"' in r.text

    def test_agents_table_shows_owner_metadata(self, admin_client):
        from app.services.agent_service import create_agent

        create_agent("owneragent", "Owner Agent", "admin")
        r = admin_client.get("/agents")
        assert "Owner:" in r.text

    def test_agents_table_shows_default_user_metadata(self, admin_client):
        from app.services.agent_service import create_agent

        create_agent("defaultagent", "Default Agent", "admin")
        r = admin_client.get("/agents")
        assert "Default user:" in r.text

    def test_agents_table_shows_access_summary_not_dead_scope_field(self, admin_client):
        r = admin_client.get("/agents")
        assert "<th>Access</th>" in r.text
        assert "<th>Scope</th>" not in r.text
        assert "a.get('scope'" not in r.text

    def test_agents_page_uses_task_based_access_copy(self, admin_client):
        r = admin_client.get("/agents")
        assert "Scope Guide" not in r.text
        assert "Agent Access" in r.text
        assert "Can Read From" in r.text
        assert "Can Write To" in r.text
        assert "private workspace" in r.text

    def test_agents_edit_modal_shows_owner_and_default_user(self, admin_client):
        r = admin_client.get("/agents")
        assert "edit-owner" in r.text
        assert "edit-default-user" in r.text

    def test_agents_create_handler_uses_scope_checkboxes(self, admin_client):
        r = admin_client.get("/agents")
        assert r.text.count("async function createAgent") == 1
        assert "getSelectedScopes('ca-read-scopes')" in r.text
        assert "document.getElementById('ca-read-scopes').value" not in r.text
        assert 'id="create-agent-error"' in r.text
        assert "Failed to create agent" in r.text
        assert "errorBox.textContent = message" in r.text
        assert "function normalizeAgentId" in r.text
        assert (
            "const agentId = normalizeAgentId(document.getElementById('ca-id').value)"
            in r.text
        )
        assert "const privateScope = 'agent:' + agentId" in r.text

    def test_agents_edit_hides_and_preserves_own_scope(self, admin_client):
        r = admin_client.get("/agents")
        assert "input.disabled = isOwnScope" in r.text
        assert "label.hidden = isOwnScope" in r.text
        assert "input:checked" in r.text
        assert "body.read_scopes.push(ownScope)" in r.text
        assert "body.write_scopes.push(ownScope)" in r.text

    def test_non_admin_agent_page_uses_projects_for_collaboration_not_user_scope_assignment(
        self, user_client
    ):
        r = user_client.get("/agents")
        assert r.status_code == 200
        assert 'data-scope="user:user1"' not in r.text
        assert "required owner context" not in r.text
        assert "enforceRequiredUserScope(containerId)" not in r.text
        assert "Use workspaces as shared collaboration spaces" in r.text

    def test_non_admin_navigation_hides_admin_pages(self, user_client):
        r = user_client.get("/")
        assert r.status_code == 200
        assert 'href="/users"' not in r.text
        assert 'href="/audit"' not in r.text


class TestUsersPage:
    def test_users_page_supports_admin_create_and_edit(self, admin_client):
        r = admin_client.get("/users")
        assert r.status_code == 200
        assert 'id="create-user-modal"' in r.text
        assert 'id="edit-user-modal"' in r.text
        assert "createUser(event)" in r.text
        assert "submitEditUser(event)" in r.text
        assert "/api/auth/users" in r.text

    def test_users_page_does_not_claim_login_registration_is_open(self, admin_client):
        r = admin_client.get("/users")
        assert "have them register at the login page" not in r.text
        assert "admins create users here" in r.text

    def test_non_admin_users_page_has_dashboard_forbidden_state(self, user_client):
        r = user_client.get("/users")
        assert r.status_code == 403
        assert "Admin Access Required" in r.text
        assert "Back to Overview" in r.text
        assert 'href="/users"' not in r.text


class TestWorkspacesPage:
    def test_workspaces_page_loads(self, admin_client):
        r = admin_client.get("/workspaces")
        assert r.status_code == 200

    def test_projects_table_has_agents_column(self, admin_client):
        r = admin_client.get("/workspaces")
        assert "<th>Agents (Read/Write)</th>" in r.text
        assert "Grant agent read or write access from Agents -> Edit." in r.text

    def test_projects_agent_access_display(self, admin_client):
        from app.services.workspace_service import create_workspace
        from app.services.agent_service import create_agent

        create_workspace("project1", "Workspace One", "admin")
        create_agent(
            "projectagent",
            "Workspace Agent",
            "admin",
            read_scopes=["agent:projectagent", "workspace:project1"],
            write_scopes=["agent:projectagent", "workspace:project1"],
        )

        r = admin_client.get("/workspaces")
        assert "projectagent" in r.text
        assert "agent-access-cell" in r.text
        assert "access-label" in r.text
        assert "scope-write" in r.text


class TestActivityPage:
    def test_activity_page_loads(self, admin_client):
        r = admin_client.get("/activity")
        assert r.status_code == 200

    def test_activity_filter_bar_present(self, admin_client):
        r = admin_client.get("/activity")
        assert "status-filter" in r.text
        assert "filterActivity" in r.text

    def test_activity_reassign_modal_present(self, admin_client):
        r = admin_client.get("/activity")
        assert 'id="reassign-modal"' in r.text
        assert "reassignActivity" in r.text

    def test_activity_reassign_button_in_table(self, admin_client):
        r = admin_client.get("/activity")
        assert "Reassign" in r.text

    def test_activity_create_sends_assigned_agent_id(self, admin_client):
        r = admin_client.get("/activity")
        assert "assigned_agent_id: agentId" in r.text
        assert "Memory Scope" in r.text
        assert "Workspace activities should usually use the workspace scope" in r.text

    def test_memory_page_clarifies_scope_and_confidence(self, admin_client):
        r = admin_client.get("/memory")
        assert "Personal user memory" in r.text
        assert "Personal user memory (user:admin)" in r.text
        assert "{user_scope" not in r.text
        assert "{user_scope_label" not in r.text
        assert "mem-search-domain" in r.text
        assert "mem-search-topic" in r.text
        assert "mem-min-confidence" in r.text
        assert "Search uses scope permissions first" in r.text
        assert "Optional exact-match search filter" in r.text
        assert "<th>Confidence</th>" in r.text

    def test_memory_page_only_offers_workflow_backed_classes(self, admin_client):
        r = admin_client.get("/memory")
        assert 'value="fact"' in r.text
        assert 'value="decision"' in r.text
        assert 'value="preference"' in r.text
        assert 'value="scratchpad"' in r.text
        assert 'value="profile"' not in r.text
        assert 'value="opinion"' not in r.text
        assert 'value="belief"' not in r.text
        assert "can be pruned by maintenance" in r.text

    def test_activity_reassign_uses_recovery_endpoint(self, admin_client):
        r = admin_client.get("/activity")
        assert "/api/activity/' + id + '/recovery" in r.text
        assert "action: 'reassign_to_agent'" in r.text


class TestAuditPage:
    def test_audit_page_loads(self, admin_client):
        r = admin_client.get("/audit")
        assert r.status_code == 200

    def test_audit_filter_bar_present(self, admin_client):
        r = admin_client.get("/audit")
        assert "audit-actor-type" in r.text
        assert "audit-action" in r.text
        assert "audit-resource" in r.text
        assert "audit-result" in r.text

    def test_audit_export_csv_button_present(self, admin_client):
        r = admin_client.get("/audit")
        assert "exportAuditCsv" in r.text
        assert "Export CSV" in r.text
        assert "/api/dashboard/audit/export" in r.text

    def test_audit_pagination_present(self, admin_client):
        r = admin_client.get("/audit")
        assert "pagination" in r.text
        assert "Prev" in r.text
        assert "Next" in r.text

    def test_audit_page_requires_admin(self, user_client):
        r = user_client.get("/audit")
        assert r.status_code == 403

    def test_audit_export_requires_admin(self, user_client):
        r = user_client.get("/api/dashboard/audit/export")
        assert r.status_code == 403

    def test_audit_export_csv_endpoint(self, admin_client):
        from app.services import audit_service

        audit_service.write_event(
            actor_type="user",
            actor_id="admin",
            action="vault_entry_created",
            resource_type="vault_entry",
            result="success",
        )
        r = admin_client.get(
            "/api/dashboard/audit/export?action=vault_entry_created&result=success"
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "vault_entry_created" in r.text

    def test_audit_page_result_filter(self, admin_client):
        from app.services import audit_service

        audit_service.write_event(
            actor_type="user",
            actor_id="admin",
            action="memory_write",
            resource_type="memory_record",
            result="success",
        )
        audit_service.write_event(
            actor_type="user",
            actor_id="admin",
            action="session_login",
            resource_type="session",
            result="failure",
        )
        r = admin_client.get("/audit?result=failure")
        assert r.status_code == 200
        assert "<code>session_login</code>" in r.text
        assert "<code>memory_write</code>" not in r.text


class TestMemoryDetailModal:
    def test_memory_detail_shows_content(self, admin_client):
        r = admin_client.get("/memory")
        assert "mem-detail-content" in r.text

    def test_memory_detail_shows_scope(self, admin_client):
        r = admin_client.get("/memory")
        assert "mem-detail-scope" in r.text

    def test_memory_detail_shows_confidence(self, admin_client):
        r = admin_client.get("/memory")
        assert "mem-detail-confidence" in r.text

    def test_memory_detail_shows_supersession_info(self, admin_client):
        r = admin_client.get("/memory")
        assert "mem-detail-supersede" in r.text
        assert "mem-chain-content" in r.text
