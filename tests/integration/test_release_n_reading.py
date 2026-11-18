"""Release N: the reading, phone and layout fixes from the third recheck.

- The approval detail page's tables fit the card on a phone.
- The Access page reads: a hint above the list, a group header that is a control, columns that do
  not move, and the group's name only in its header.
- A stacked table keeps the value in its own column, so a value that wraps lines up under itself.
- The permission notice names the agents that need the permission.
- The audit snapshot says that its three lists count the page that is shown.
- The small faults: the whole request on the Activity page, the Security table on a phone, the
  sidebar mark in the dark theme, and the space above the owner picker.
"""

from pathlib import Path

import pytest

from app.services import activity_service, agent_service, approval_service, audit_service, workspace_service
from app.services.auth_service import create_session, create_user

REPO = Path(__file__).resolve().parents[2]
CSS = REPO / "app" / "dashboard" / "static" / "css" / "dashboard.css"
ACCESS_PAGE_JS = REPO / "app" / "dashboard" / "static" / "js" / "access_page.js"
APPROVALS_PAGE_PY = REPO / "app" / "routes" / "approvals_page.py"


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


def _css_rule(text: str, selector: str) -> str:
    """The whole rule for one selector, braces and all."""
    start = text.index(selector)
    return text[start:text.index("}", start) + 1]


def _phone_rules(text: str, selector: str) -> str:
    """The rule for one selector inside the phone block, braces and all."""
    block = text.split("@media (max-width: 768px)")
    for part in block[1:]:
        if selector in part:
            start = part.index(selector)
            return part[start:part.index("}", start) + 1]
    raise AssertionError(f"{selector} is not in a phone block")


# ── the approval detail page on a phone ─────────────────────────────────────


def test_the_approval_detail_tables_are_the_ones_that_fit_a_phone(admin):
    """Every table on the detail page is a detail table: the phone rules reach all of them."""
    workspace_service.create_workspace("rel-ws", "Release Place", "admin")
    agent_service.create_agent(
        "rel-agent", "Release Agent", "admin",
        read_scopes=["workspace:rel-ws"], write_scopes=["workspace:rel-ws"], capabilities=["memory", "admin_propose"],
    )
    agent_service.create_agent(
        "rel-target", "Release Target", "admin",
        read_scopes=["workspace:rel-ws"], write_scopes=[], capabilities=["memory"],
    )
    proposal = approval_service.propose(
        agent_id="rel-agent", scope="workspace:rel-ws", title="Widen the phone layout",
        operations=[{"name": "agent.update", "params": {
            "agent_id": "rel-target", "display_name": "Release Target",
            "description": "A description long enough that it has to wrap on a narrow phone screen",
            "read_scopes": ["workspace:rel-ws"], "write_scopes": [], "capabilities": ["memory"],
        }}],
    )
    html = admin.get(f"/approvals/{proposal['id']}").text
    assert html.count("detail-table") == 2  # the meta table and the operation table
    # The value is in the page, in full.
    assert "A description long enough that it has to wrap on a narrow phone screen" in html


def test_the_detail_tables_keep_their_two_columns_and_wrap_inside_them():
    """On a phone the tables are as wide as their card, the columns keep their places, and the cells
    wrap inside their own column."""
    rule = _phone_rules(CSS.read_text(), ".detail-table {")
    assert "display: table" in rule
    assert "width: 100%" in rule
    assert "table-layout: fixed" in rule
    cells = _phone_rules(CSS.read_text(), ".detail-table th, .detail-table td {")
    assert "overflow-wrap: anywhere" in cells
    assert "white-space: normal" in cells
    label = _phone_rules(CSS.read_text(), ".detail-table th:first-child, .detail-table td:first-child {")
    assert "width: 8.5rem" in label
    result = _phone_rules(CSS.read_text(), ".detail-table pre {")
    assert "white-space: pre-wrap" in result
    assert "overflow-wrap: anywhere" in result
    assert "max-width: 100%" in result


def test_every_table_on_the_approval_detail_page_carries_the_class():
    source = APPROVALS_PAGE_PY.read_text()
    # The meta table, the operation table, the decision table and the connector request table.
    assert source.count("class='detail-table'") == 3
    assert source.count('class="detail-table"') == 1
    # The label column is not pinned to a fixed width any more: the phone rules give it 8.5rem.
    assert "width:180px" not in source
    assert "width:9rem" not in source


# ── the Access page reads ───────────────────────────────────────────────────


def test_the_access_page_says_what_to_do_with_the_list(admin):
    """One plain line above the list, under the filters, and room between the intro and the card."""
    html = admin.get("/access").text
    assert "Select a name to see what it reaches." in html
    # It sits above the row count, which is above the table.
    assert html.index("Select a name to see what it reaches.") < html.index('id="access-row-count"')
    assert html.index('id="access-row-count"') < html.index('id="access-table-wrap"')
    # The intro line has room under it.
    assert 'style="max-width:760px;margin-top:8px;margin-bottom:12px"' in html


def test_the_access_table_keeps_its_columns_in_place(admin):
    """The columns have their own widths, so opening a group moves nothing."""
    html = admin.get("/access").text
    assert 'class="stack-phone access-table"' in html
    assert "<colgroup>" in html
    for width in ("26%", "24%", "18%", "20%", "12%"):
        assert f'<col style="width:{width}">' in html, width
    assert "table-layout: fixed" in _css_rule(CSS.read_text(), ".access-table {")


def test_the_group_header_reads_as_a_control():
    """The header row has a background, a rule under it, a pointer, and a stronger hover."""
    css = CSS.read_text()
    head = _css_rule(css, ".access-group-head {")
    assert "background: rgba(127, 127, 127, 0.07)" in head
    assert "cursor: pointer" in head
    hover = _css_rule(css, ".access-group-head:hover {")
    assert "rgba(127, 127, 127, 0.12)" in hover
    cell = _css_rule(css, ".access-group-head td {")
    assert "border-bottom: 1px solid var(--border)" in cell
    # The arrow is bigger and in the ordinary text colour.
    chevron = _css_rule(css, ".access-chevron {")
    assert "font-size: 1.1em" in chevron
    assert "color: var(--text)" in chevron


def test_the_group_header_keeps_the_kind_whole_beside_a_long_name():
    """The kind and the count never break; the name wraps inside its own column."""
    css = CSS.read_text()
    quiet = _css_rule(css, ".access-group-toggle .text-muted {")
    assert "white-space: nowrap" in quiet
    assert "flex: none" in quiet
    name = _css_rule(css, ".access-group-toggle strong {")
    assert "min-width: 0" in name
    assert "overflow-wrap: anywhere" in name
    count = _css_rule(css, ".access-group-count {")
    assert "white-space: nowrap" in count
    assert "flex: none" in count


def test_the_group_header_row_opens_the_group_and_the_button_does_not_toggle_it_twice():
    """The row has its own click, and the button stops the click before it reaches the row."""
    source = ACCESS_PAGE_JS.read_text()
    head = source.split("function groupHead", 1)[1].split("\n  }", 1)[0]
    assert "tr.addEventListener('click'" in head
    assert "ev.stopPropagation()" in head
    assert "ev.target.closest" in head


def test_the_name_of_the_group_is_only_in_its_header():
    """The rows under a header leave the first cell empty: the header names the group."""
    source = ACCESS_PAGE_JS.read_text()
    row = source.split("function buildRow", 1)[1].split("\n  }", 1)[0]
    # The first cell is made empty, and the name is not written into it again.
    assert "who.setAttribute('data-label', 'Who or what')" in row
    assert "partyCell(row.from" not in row
    assert "partyCell(row.to, 'Reaches')" in row


def test_the_services_page_intro_has_room_under_it():
    """The intro line on the Services page does not touch the card below it."""
    source = (REPO / "app" / "routes" / "agents_page.py").read_text()
    assert 'class=\\"text-muted\\" style=\\"margin-bottom:12px\\"' in source


# ── a stacked table keeps the value in its own column ───────────────────────


def test_a_wrapped_value_lines_up_under_itself():
    """The label has a column of its own and the value keeps it: a value that wraps starts under
    the value, not under the label."""
    css = CSS.read_text()
    cell = _phone_rules(css, ".stack-phone td[data-label] {")
    assert "position: relative" in cell
    assert "padding-left: calc(8rem + 8px)" in cell
    label = _phone_rules(css, ".stack-phone td[data-label]::before {")
    assert "position: absolute" in label
    assert "left: 0" in label
    assert "width: 8rem" in label
    assert "display: block" in label
    # The label keeps its own look.
    assert "color: var(--muted)" in label
    assert "font-size: 0.75rem" in label
    assert "text-transform: uppercase" in label
    assert "letter-spacing: 0.05em" in label
    # A cell without a label (an action cell) keeps no padding of its own.
    plain = _phone_rules(css, ".stack-phone td { border: 0;")
    assert "padding: 3px 0" in plain
    assert "padding-left" not in plain


def test_the_pages_that_stack_say_what_each_cell_holds(admin):
    """Every stacked table on a phone names the value it holds, so the label is never a mystery."""
    workspace_service.create_workspace("rel-ws", "Release Place", "admin")
    html = admin.get("/users").text
    assert 'class="stack-phone"' in html
    for label in ("Name", "Email", "Role", "Status", "Two-step sign-in", "Created", "Actions"):
        assert f"data-label='{label}'" in html, label


# ── the permission notice names who ─────────────────────────────────────────


def test_the_permission_notice_is_given_the_names_of_the_agents_that_need_it():
    """The notice is told which agents need the permission, so it can name them."""
    for path in (REPO / "app" / "dashboard" / "static" / "js" / "agent_access.js",
                 REPO / "app" / "dashboard" / "static" / "js" / "credentials.js"):
        source = path.read_text()
        assert "function accessNamesOf(rows)" in source, path.name
        assert "row.display_name" in source, path.name
        assert "accessNamesOf(group.rows)" in source, path.name


# ── the audit snapshot ──────────────────────────────────────────────────────


def test_the_audit_snapshot_says_that_its_lists_count_the_page(admin):
    """The three lists count the page that is shown, like the tiles do."""
    audit_service.write_event(
        actor_type="user", actor_id="admin", action="workspace_created",
        resource_type="workspace", resource_id="rel-ws", result="success",
    )
    html = admin.get("/audit").text
    assert "Top actors on this page" in html
    assert "Top actions on this page" in html
    assert "Top resources on this page" in html
    assert "Top actors:" not in html


# ── the small faults ────────────────────────────────────────────────────────


def test_the_waiting_on_you_list_shows_the_whole_request(admin):
    """The request is shown whole: nothing is cut off in the middle of a word."""
    agent_service.create_agent(
        "rel-asker", "Release Asker", "admin",
        read_scopes=["workspace:rel-ws"], write_scopes=["workspace:rel-ws"], capabilities=["memory"],
    )
    long_task = "A task description that runs on and on and is cut in the middle of a word by the page"
    ask = activity_service.create_activity(
        agent_id="rel-asker", user_id="admin", task_description=long_task, memory_scope="workspace:rel-ws",
    )
    activity_service.set_handoff(ask["id"], {"needs_human": "Please answer this question."})
    activity_service.update_activity(ask["id"], status="blocked", task_result="Waiting on a decision")
    html = admin.get("/activity").text
    assert long_task in html
    # The request keeps its own lines and wraps inside its cell.
    assert "activity-request" in html
    source = (REPO / "app" / "routes" / "activity_page.py").read_text()
    assert "task_description', '')[:100]" not in source
    assert "white-space: pre-line" not in source


def test_the_security_table_stacks_on_a_phone(admin):
    """The table under Security says what each cell holds, so it does not squeeze its Notes."""
    html = admin.get("/settings").text
    security = html.split('id="tab-security"')[1].split("</section>")[0]
    assert 'class="stack-phone"' in security
    for label in ("Item", "Status", "Notes"):
        assert f"data-label='{label}'" in security, label


def test_the_new_owner_label_has_room_above_it():
    """The owner picker's label does not touch the row above it."""
    source = (REPO / "app" / "routes" / "credentials_page.py").read_text()
    panel = source.split('id="access-owner-panel"', 1)[1].split("</div>", 1)[0]
    assert 'class="form-group" style="margin-top:12px"' in panel


def test_the_sidebar_mark_reads_in_the_dark_theme():
    """The mark sits on a light backing, and the dark theme lightens it."""
    css = CSS.read_text()
    logo = _css_rule(css, ".brand-logo {")
    assert "background:" in logo
    dark = _css_rule(css, '[data-theme="dark"] .brand-logo {')
    assert "filter: brightness" in dark
