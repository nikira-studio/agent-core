"""Release L: the layout, wording and dialog fixes.

- The Access dialogs say once what a list is missing, with one Turn on button.
- The Activity table keeps its note inside its column; the tables stack at phone width.
- The wrong words are gone and the right ones are in their place.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import connector_service, workspace_service
from app.services.auth_service import create_session, create_user

REPO = Path(__file__).resolve().parents[2]
AGENT_ACCESS_JS = REPO / "app" / "dashboard" / "static" / "js" / "agent_access.js"
CREDENTIALS_JS = REPO / "app" / "dashboard" / "static" / "js" / "credentials.js"
ACCESS_PAGE_JS = REPO / "app" / "dashboard" / "static" / "js" / "access_page.js"


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


@pytest.fixture
def world(admin):
    """An admin, a workspace, an agent, a credential and a connection."""
    workspace_service.create_workspace("acc", "Accounts", "admin")
    return admin


# ── one notice and one Turn on ─────────────────────────────────────────────


def test_the_agent_dialog_says_once_what_its_lists_are_missing():
    """The list says what it needs once, above the rows; the rows keep a quiet note of their own."""
    source = AGENT_ACCESS_JS.read_text()
    # The notice is told how many rows need the permission and how many rows the list has,
    # so the sentence can say what is true: one row, all of them, or only some.
    assert "noticeText(group.label, group.rows.length, total, accessNamesOf(group.rows))" in source
    assert "'This needs the ' + label + ' permission before this agent can use it.'" in source
    assert "'One of these needs the ' + label + ' permission before this agent can use it.'" in source
    assert "'These need the ' + label + ' permission before this agent can use them.'" in source
    assert "'Some of these need the ' + label + ' permission before this agent can use them.'" in source
    # One button per missing permission, and it names what it turns on.
    assert "'Turn on ' + group.label" in source
    # The rows keep a plain note, not a badge and not a button of their own.
    for name in ("renderAgentCapabilities", "renderAgentCredentials", "renderConnectionUsersNow"):
        body = source.split("function " + name, 1)[1].split("\nfunction ", 1)[0]
        assert "accessNeedsChip" not in body, name
        assert "'needs permission'" in body, name
        assert "Turn on" not in body, name


def test_the_credential_dialog_says_once_what_its_lists_are_missing():
    source = CREDENTIALS_JS.read_text()
    # The same sentences, counted the same way, for a list of other agents: the agents that need the
    # permission are named.
    assert "accessAgentsNoticeText(group.label, group.rows.length, total, accessNamesOf(group.rows))" in source
    assert "who[0] + ' needs the ' + label + ' permission before it can use it.'" in source
    assert "accessNameList(who) + ' need the ' + label + ' permission before they can use it.'" in source
    assert "'All of these agents need the ' + label + ' permission before they can use it.'" in source
    assert "'Turn on ' + group.label" in source
    for name in ("renderAccessAgents", "connectionUsers"):
        body = source.split("function " + name, 1)[1].split("\nfunction ", 1)[0]
        assert "'needs permission'" in body, name
        assert "'needs permission: '" not in body, name
        assert "turnOnButton" not in body, name


def test_the_turn_on_button_calls_the_same_route_as_before():
    """What Turn on does is not changed: it is the same recorded action, on the same route."""
    for path in (AGENT_ACCESS_JS, CREDENTIALS_JS):
        source = path.read_text()
        assert "'/permissions/' + encodeURIComponent(" in source
        assert "'/turn-on'" in source


def test_the_credential_rows_name_the_owner_once():
    """A row reads "<name> <owner> <reason>"; when the reason already names the owner, it is not named twice."""
    source = AGENT_ACCESS_JS.read_text()
    assert "reason === 'through ' + owner" in source
    # A person's own space is named by the reason as well ("acts for <person>"): it is not named twice either.
    assert "reason.indexOf('acts for ') === 0" in source
    assert "owner.endsWith(\"'s personal space\")" in source


# ── the owner panel ────────────────────────────────────────────────────────


def test_the_owner_panel_starts_with_nothing_to_save(admin):
    """Save is off until the owner chosen is not the one it has now."""
    html = admin.get("/credentials").text
    assert 'id="access-owner-save" disabled' in html
    source = CREDENTIALS_JS.read_text()
    assert "Nothing changes." in source
    assert "save.disabled = !changed" in source


def test_the_owner_panel_is_scrolled_into_view():
    source = CREDENTIALS_JS.read_text()
    assert "scrollIntoView({ block: 'nearest' })" in source


# ── the pages that stack on a phone ────────────────────────────────────────


@pytest.mark.parametrize(
    "route,marker",
    [
        ("/", "id=\"overview-waiting\""),
        ("/approvals", "Waiting for a decision"),
        ("/access", "class=\"stack-phone access-table\""),
    ],
)
def test_the_tables_stack_on_a_phone(admin, route, marker):
    html = admin.get(route).text
    assert marker in html


def test_the_activity_waiting_table_stacks(admin):
    """The Activity page's own waiting table is one block per row on a phone."""
    from app.services import activity_service, agent_service

    agent_service.create_agent("asker", "Asker", "admin", read_scopes=["workspace:acc"])
    ask = activity_service.create_activity(
        agent_id="asker", user_id="admin", task_description="Ask a person", memory_scope="workspace:acc"
    )
    activity_service.set_handoff(ask["id"], {"needs_human": "Please answer."})
    html = admin.get("/activity").text
    assert "Waiting on you" in html
    assert "<table class='stack-phone'>" in html
    assert "data-label='Request'" in html
    assert "data-label='Action'" in html


def test_the_approvals_page_names_the_workspace(admin):
    """The Workspace column shows the workspace's name, never the scope it is stored under."""
    from app.services import approval_service, agent_service

    workspace_service.create_workspace("ops", "Ops", "admin")
    agent_service.create_agent("proposer", "Proposer", "admin", read_scopes=["workspace:ops"])
    agent_service.create_agent("target", "Target", "admin", read_scopes=["workspace:ops"])
    approval_service.propose(
        agent_id="proposer", scope="workspace:ops", title="Widen the target",
        operations=[{"name": "agent.update", "params": {
            "agent_id": "target", "display_name": "Target", "description": "Wider",
            "read_scopes": ["workspace:ops"], "write_scopes": [], "capabilities": ["memory"],
        }}],
    )
    html = admin.get("/approvals").text
    assert ">Ops<" in html
    assert "workspace:ops" not in html


# ── the Activity table ─────────────────────────────────────────────────────


def test_the_activity_note_sits_under_the_task_title(admin):
    """The note under the Status badge used to run over the columns next to it."""
    source = (REPO / "app" / "routes" / "activity_page.py").read_text()
    assert "activity-ask-note" in source
    # The note is in the task's cell, not in the status cell.
    task_cell = source.split("def _task_cell", 1)[1].split("def ", 1)[0]
    assert "_ask_note(a)" in task_cell
    status_cell = source.split("activity-status-cell", 1)[1].split("\n", 1)[0]
    assert "_ask_note" not in status_cell


# ── the Access page ────────────────────────────────────────────────────────


def test_the_unused_card_is_above_the_table(admin):
    html = admin.get("/access").text
    assert html.index("<h2>Unused</h2>") < html.index("<h2>Who reaches what</h2>")
    # Both loading states are still there.
    assert html.count("Loading...") == 2


def test_the_access_page_says_never_and_not_never_seen():
    source = ACCESS_PAGE_JS.read_text()
    assert "'never seen'" not in source
    assert "'never'" in source


def test_the_access_unused_lists_show_one_line_of_why():
    """The stray bare "never" line above the sentence is gone: the name, then the sentence."""
    source = ACCESS_PAGE_JS.read_text()
    assert "last signed in " not in source
    assert "var lines = item.why ? [item.why] : [];" in source


def test_the_last_used_column_does_not_wrap():
    source = ACCESS_PAGE_JS.read_text()
    assert "access-last-used" in source
    css = (REPO / "app" / "dashboard" / "static" / "css" / "dashboard.css").read_text()
    assert ".access-last-used { white-space: nowrap; }" in css


# ── the wrong words ────────────────────────────────────────────────────────


def test_the_service_form_says_what_a_service_is_for():
    """The Description hint follows the thing it is about: an agent, or a service."""
    source = (REPO / "app" / "routes" / "agents_page.py").read_text()
    assert "A reminder of what this agent is for." in source
    assert "A reminder of what this service is for." in source
    assert 'id="ca-description-hint"' in source
    assert 'id="edit-description-hint"' in source


def test_the_capabilities_page_does_not_say_what_it_used_to_be_called(admin):
    html = admin.get("/connectors").text
    assert "Previously called Services" not in html


def test_the_credential_dialog_names_its_first_group_connections(admin):
    html = admin.get("/credentials").text
    assert "Connections that sign in with this" in html
    assert "Add a connection" in html


def test_a_signed_in_oauth_connection_offers_re_authorize(admin):
    """The blue button is for a connection that is not signed in yet; one that is offers the quiet way back."""
    connector_service.create_connector_type(
        connector_type_id="google_sheets", display_name="Sheets", auth_type="oauth2", provider_type="builtin",
        supported_actions=["read"], backend_type="http",
        backend_json=json.dumps({"type": "http", "requests": {}}),
    )
    binding = connector_service.create_binding(
        "google_sheets", "Account", "workspace:acc", config_json='{"base_url": "http://example.test"}'
    )
    html = admin.get("/connectors").text
    assert "Authorize Google" in html
    assert "Re-authorize Google" not in html

    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE connector_bindings SET last_tested_at = ? WHERE id = ?",
            ("2026-11-01T10:00:00+00:00", binding["id"]),
        )
        conn.commit()
    html = admin.get("/connectors").text
    assert "Re-authorize Google" in html
    assert ">Authorize Google<" not in html


# ── the scripts still parse ────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", [AGENT_ACCESS_JS, CREDENTIALS_JS, ACCESS_PAGE_JS])
def test_the_scripts_are_valid_javascript(path):
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"{path.name}: {result.stderr}"
