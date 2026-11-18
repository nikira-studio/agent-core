"""Release M: the layout and wording fixes from the second recheck.

- The Waiting on you list gives the request the whole width, and its button stays inside the card.
- The Audit log and the Workspaces table stack on a phone, like the other tables.
- The label in front of a value has room of its own.
- One notice per list says what is true: one row, every row, or only some.
- The audit snapshot says what its numbers count.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import activity_service, agent_service, audit_service, workspace_service
from app.services.auth_service import create_session, create_user

REPO = Path(__file__).resolve().parents[2]
CSS = REPO / "app" / "dashboard" / "static" / "css" / "dashboard.css"
AGENT_ACCESS_JS = REPO / "app" / "dashboard" / "static" / "js" / "agent_access.js"
CREDENTIALS_JS = REPO / "app" / "dashboard" / "static" / "js" / "credentials.js"


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


@pytest.fixture
def world(admin):
    """An admin, a workspace and an agent with a question waiting for a person."""
    workspace_service.create_workspace("rm-ws", "Release Place", "admin")
    agent_service.create_agent(
        "rm-agent", "Release Agent", "admin",
        read_scopes=["workspace:rm-ws"], write_scopes=["workspace:rm-ws"], capabilities=["memory"],
    )
    ask = activity_service.create_activity(
        agent_id="rm-agent", user_id="admin", task_description="Ask a person",
        memory_scope="workspace:rm-ws",
    )
    activity_service.set_handoff(ask["id"], {"needs_human": "Please answer this question."})
    activity_service.update_activity(ask["id"], status="blocked", task_result="Waiting on a decision")
    return admin


def _css_rule(text: str, selector: str) -> str:
    """The whole rule for one selector, braces and all."""
    start = text.index(selector)
    return text[start:text.index("}", start) + 1]


# ── the Waiting on you list ────────────────────────────────────────────────


def test_the_request_takes_the_whole_width_and_the_button_stays_inside_the_card(world):
    """The row is a block: the request on top, the answer under it, the button beside the answer."""
    html = world.get("/").text
    waiting = html.split('id="overview-waiting"', 1)[1].split("</table>", 1)[0]
    assert "class='stack-phone waiting-table'" in waiting
    assert "data-label='Request'" in waiting and "data-label='Action'" in waiting
    assert "Mark as dealt with" in waiting

    css = CSS.read_text()
    # The rows are stacked at every width above a phone's, so the request has the whole width.
    assert _css_rule(
        css, ".waiting-table, .waiting-table tbody, .waiting-table tr, .waiting-table td"
    ) == ".waiting-table, .waiting-table tbody, .waiting-table tr, .waiting-table td { display: block; width: 100%; }"
    # The answer box and its button share one line, and the box does not grow without end.
    assert "display: flex" in _css_rule(css, ".waiting-table .inline-answer {")
    assert "max-width: 520px" in _css_rule(css, ".waiting-table .inline-answer input {")
    # The action cell is not one long line: that is what pushed the button off the card.
    rule = _css_rule(css, ".waiting-table td { border: 0;")
    assert "white-space: normal" in rule and "text-align: left" in rule


def test_a_phone_keeps_its_own_stacked_rows(world):
    """The phone rules are untouched: the table still stacks below 769px."""
    css = CSS.read_text()
    assert _css_rule(
        css, ".stack-phone, .stack-phone tbody, .stack-phone tr, .stack-phone td"
    ) == ".stack-phone, .stack-phone tbody, .stack-phone tr, .stack-phone td { display: block; width: 100%; }"


# ── the tables that stack on a phone ───────────────────────────────────────


def test_the_audit_log_stacks_on_a_phone(world):
    """The audit rows are one block per event, and every cell says what it holds."""
    audit_service.write_event(
        actor_type="user", actor_id="admin", action="workspace_created",
        resource_type="workspace", resource_id="rm-ws", result="success",
        details={"note": "a release M row"},
    )
    html = world.get("/audit").text
    assert 'class="audit-table stack-phone"' in html
    for label in ("Time", "Who", "Action", "Resource", "Result", "Details", "IP"):
        assert f"data-label='{label}'" in html, label


def test_the_workspaces_table_stacks_on_a_phone(world):
    """The workspace rows are one block per workspace, and the buttons are still there."""
    html = world.get("/workspaces").text
    assert '<table class="stack-phone">' in html
    for label in ("ID", "Name", "Status", "Owner", "Memories", "Agents", "Created", "Actions"):
        assert f"data-label='{label}'" in html, label
    assert "data-workspace-action='edit'" in html


def test_a_stacked_cell_has_no_minimum_width_that_would_push_it_off_the_screen():
    """A cell takes the width of the card, whatever its column asked for."""
    rule = _css_rule(CSS.read_text(), ".stack-phone td { border: 0;")
    assert "min-width: 0" in rule
    assert "overflow-wrap: anywhere" in rule


def test_the_label_in_front_of_a_value_has_room_of_its_own():
    """The label has a column of its own, and the value keeps that column: a value that wraps starts
    under the value, not under the label."""
    css = CSS.read_text()
    rule = _css_rule(css, ".stack-phone td[data-label]::before {")
    assert "width: 8rem" in rule
    assert "position: absolute" in rule
    cell = _css_rule(css, ".stack-phone td[data-label] {")
    assert "padding-left: calc(8rem + 8px)" in cell


# ── the Access page ────────────────────────────────────────────────────────


def test_the_unused_note_has_room_under_it(world):
    """The sentence about unused things is not tight against the next heading."""
    html = world.get("/access").text
    assert "Things nothing uses. Check each before you delete it.</p>" in html
    assert "margin-bottom:12px\">Things nothing uses" in html
    # The unused lists carry a long name that wraps inside its own line.
    assert html.count('class="access-unused-list"') == 4
    css = CSS.read_text()
    assert ".access-unused-list li { overflow-wrap: anywhere; }" in css


# ── one notice per list says what is true ──────────────────────────────────


def test_the_agent_notice_says_whether_it_is_one_row_all_of_them_or_some():
    """The notice counts the rows that need the permission, so the sentence is true."""
    source = AGENT_ACCESS_JS.read_text()
    notice = source.split("function accessAgentNoticeText", 1)[1].split("\n}", 1)[0]
    assert "needed === 1" in notice
    assert "needed === total" in notice
    assert "This needs the ' + label + ' permission before this agent can use it." in notice
    assert "One of these needs the ' + label + ' permission before this agent can use it." in notice
    assert "These need the ' + label + ' permission before this agent can use them." in notice
    assert "Some of these need the ' + label + ' permission before this agent can use them." in notice
    # The rows that need it, the rows in the list, and the agents that need it are all passed in.
    assert "noticeText(group.label, group.rows.length, total, accessNamesOf(group.rows))" in source
    assert "list.length" in source and "users.length" in source


def test_the_notice_for_other_agents_names_the_agents_that_need_it():
    """A list of other agents says who needs the permission: one of them by name, several of them by name."""
    source = AGENT_ACCESS_JS.read_text()
    notice = source.split("function accessOthersNoticeText", 1)[1].split("\n}", 1)[0]
    assert "who.length === 1" in notice
    assert "needed === total" in notice
    assert "who[0] + ' needs the ' + label + ' permission before it can use it.'" in notice
    assert "All of these agents need the ' + label + ' permission before they can use it." in notice
    assert "accessNameList(who) + ' need the ' + label + ' permission before they can use it." in notice
    # The names are the agents that need it, each once, in the order the list shows them.
    assert "function accessNamesOf(rows)" in source
    assert "row.display_name" in source

    credentials = CREDENTIALS_JS.read_text()
    notice = credentials.split("function accessAgentsNoticeText", 1)[1].split("\n}", 1)[0]
    assert "who.length === 1" in notice
    assert "needed === total" in notice
    assert "who[0] + ' needs the ' + label + ' permission before it can use it.'" in notice
    assert "All of these agents need the ' + label + ' permission before they can use it." in notice
    assert "accessNameList(who) + ' need the ' + label + ' permission before they can use it." in notice
    assert "accessAgentsNoticeText(group.label, group.rows.length, total, accessNamesOf(group.rows))" in credentials


def test_the_turn_on_button_still_does_the_same_thing():
    """Only the sentence changed: the button turns the same permission on, the same way."""
    for path in (AGENT_ACCESS_JS, CREDENTIALS_JS):
        source = path.read_text()
        assert "'Turn on ' + group.label" in source
        assert "'/permissions/' + encodeURIComponent(" in source
        assert "'/turn-on'" in source


# ── the agent's own rows ───────────────────────────────────────────────────


def test_a_row_names_the_person_once():
    """The reason already says "acts for <person>": the owner's name is not said again."""
    source = AGENT_ACCESS_JS.read_text()
    body = source.split("function renderAgentCredentials", 1)[1].split("\nfunction ", 1)[0]
    assert "reason === 'through ' + owner" in body
    assert "reason.indexOf('acts for ') === 0" in body
    assert "owner.endsWith(\"'s personal space\")" in body


# ── the audit snapshot ─────────────────────────────────────────────────────


def test_the_snapshot_says_what_its_numbers_count(world):
    """The total is the whole log; the other three count the page that is shown."""
    audit_service.write_event(
        actor_type="user", actor_id="admin", action="workspace_created",
        resource_type="workspace", resource_id="rm-ws", result="success",
    )
    html = world.get("/audit").text
    assert "<div class=\"label\">Total Events</div>" in html
    assert "<div class=\"label\">Success on this page</div>" in html
    assert "<div class=\"label\">Failure on this page</div>" in html
    assert "<div class=\"label\">Blocked on this page</div>" in html


# ── the scripts still parse ────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", [AGENT_ACCESS_JS, CREDENTIALS_JS])
def test_the_scripts_are_valid_javascript(path):
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_the_activity_waiting_list_is_unchanged():
    """The Activity page's own waiting table has no answer box, so it never had the fault."""
    source = (REPO / "app" / "routes" / "activity_page.py").read_text()
    waiting = source.split("waiting_rows = ", 1)[1].split("superseded = ", 1)[0]
    assert "data-label='Request'" in waiting
    assert "data-label='Action'" in waiting
    # No actions cell, so no one-long-line cell: the button was never pushed off the card.
    assert "actions-cell" not in waiting


def test_the_users_table_stacks_on_a_phone(test_client, admin_token):
    from tests.integration.test_approvals import _browser_admin

    _browser_admin(test_client)
    html = test_client.get("/users").text
    assert 'class="stack-phone"' in html
    for label in ("Name", "Email", "Role", "Status", "Two-step sign-in", "Created", "Actions"):
        assert f"data-label='{label}'" in html
