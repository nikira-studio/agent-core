"""Nothing an agent or user can type may reach the dashboard as markup.

Agents write their own task descriptions, users name their own workspaces and
agents, and remote endpoints supply webhook error messages. All of it is
rendered on pages an admin opens, so any of it is a path to running script in
an admin's session.
"""

import pytest

from app.routes.dashboard_shared import escape_html

PAYLOAD = "<script>alert(1)</script>"
ATTR_PAYLOAD = "' onmouseover='alert(1)"


def _admin(token):
    return {"Authorization": f"Bearer {token}"}


# --- the shared escaper ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<script>", "&lt;script&gt;"),
        ('"', "&quot;"),
        ("'", "&#39;"),
        ("&", "&amp;"),
        (None, ""),
    ],
)
def test_the_escaper_covers_both_quote_styles(raw, expected):
    """Single quotes matter: this dashboard uses single-quoted attributes."""
    assert escape_html(raw) == expected


def test_the_escaper_neutralises_an_attribute_breakout():
    assert "'" not in escape_html(ATTR_PAYLOAD)


# --- agent-supplied task descriptions --------------------------------------


def test_a_task_description_cannot_inject_script(test_client, admin_token, agent_token):
    r = test_client.post(
        "/mcp",
        headers=_admin(agent_token),
        json={
            "tool": "activity_update",
            "params": {"task_description": PAYLOAD, "status": "active"},
        },
    )
    assert r.status_code in (200, 201), r.json()

    for page in ("/activity", "/"):
        page_html = test_client.get(page, headers=_admin(admin_token)).text
        assert PAYLOAD not in page_html, f"{page} rendered the payload as markup"
        assert "&lt;script&gt;" in page_html or "alert(1)" not in page_html


def test_a_task_description_cannot_break_out_of_an_attribute(
    test_client, admin_token, agent_token
):
    test_client.post(
        "/mcp",
        headers=_admin(agent_token),
        json={
            "tool": "activity_update",
            "params": {"task_description": ATTR_PAYLOAD, "status": "active"},
        },
    )
    page_html = test_client.get("/activity", headers=_admin(admin_token)).text
    assert "onmouseover='alert(1)" not in page_html


# --- user-supplied names ---------------------------------------------------


def test_a_workspace_name_cannot_inject_script(test_client, admin_token):
    r = test_client.post(
        "/api/workspaces",
        headers=_admin(admin_token),
        json={"id": "xssproj", "name": PAYLOAD, "description": "x"},
    )
    assert r.status_code == 201, r.json()

    for page in ("/workspaces", "/agents"):
        assert PAYLOAD not in test_client.get(page, headers=_admin(admin_token)).text


def test_an_agent_display_name_cannot_inject_script(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers=_admin(admin_token),
        json={"id": "xssagent", "display_name": PAYLOAD, "description": "x"},
    )
    assert r.status_code in (200, 201), r.json()
    assert PAYLOAD not in test_client.get("/agents", headers=_admin(admin_token)).text


def test_a_user_display_name_cannot_inject_script(test_client, admin_token):
    r = test_client.post(
        "/api/auth/users",
        headers=_admin(admin_token),
        json={
            "id": "xssuser",
            "email": "xss@test.local",
            "password": "testpassword123",
            "display_name": PAYLOAD,
            "role": "user",
        },
    )
    assert r.status_code in (200, 201), r.json()
    assert PAYLOAD not in test_client.get("/users", headers=_admin(admin_token)).text


# --- client-rendered rows --------------------------------------------------


def test_no_page_ships_a_weaker_local_escaper(test_client, admin_token):
    """Two pages shadowed the global helper with one that ignored quotes.

    A local copy is not wrong in itself, but a local copy that escapes less
    than the shared one silently downgrades every call site on that page.
    """
    import pathlib

    weak = "replace(/>/g,'&gt;');"
    for path in pathlib.Path("app/routes").glob("*_page.py"):
        source = path.read_text()
        if "function escapeHtml" in source:
            assert "&#39;" in source or "&#x27;" in source, (
                f"{path} defines an escapeHtml that does not escape quotes"
            )
        assert weak not in source, f"{path} still ships the weak escaper"


def test_delivery_rows_escape_remote_error_text(test_client, admin_token):
    page = test_client.get("/webhooks", headers=_admin(admin_token)).text
    assert "escapeHtml(d.error_message)" in page
    assert "${d.error_message}" not in page


# --- escaping is not enough inside JavaScript ------------------------------


def _js_sources():
    import pathlib

    return list(pathlib.Path("app/dashboard/static/js").glob("*.js")) + list(
        pathlib.Path("app/routes").glob("*_page.py")
    )


def test_no_static_script_ships_a_weaker_escaper():
    """The earlier audit only looked at route files, so the static ones were missed."""
    import pathlib

    for path in pathlib.Path("app/dashboard/static/js").glob("*.js"):
        source = path.read_text()
        if "function escapeHtml" in source:
            assert "&#39;" in source or "&#x27;" in source, (
                f"{path} defines an escapeHtml that leaves apostrophes intact"
            )


def test_no_dynamic_value_is_spliced_into_an_inline_handler():
    """HTML escaping does not protect a JavaScript context.

    Entities in an attribute are decoded before the handler is parsed, so an
    escaped quote becomes a real one and ends the argument early. The fix is
    not a better escaper — it is carrying the value as data and reading it back
    from the dataset.
    """
    import re

    # Scan whole files, not lines: a handler attribute can span lines, and the
    # closing delimiter may be escaped (`onclick=\"...\"` inside an f-string).
    # Matching a line at a time also flags neighbouring attributes that are
    # perfectly safe.
    # Newlines are allowed inside the body so a wrapped attribute is still seen,
    # but it stops at `>`: a handler attribute cannot contain an unescaped one,
    # and without that bound the match runs on into unrelated code.
    # `(?<![\w-])` so the attribute name starts here: without it, the tail of
    # `data-otp-confirm=` reads as an `onfirm=` handler.
    handler = re.compile(r"""(?<![\w-])on\w+\s*=\s*\\?(["'])((?:(?!\1)[^>])*)""")
    # Every way a value gets into that body:
    #   {name}          f-string interpolation
    #   ${name}         template literal
    #   ' + something   JavaScript concatenation, in either quote style
    interpolated = re.compile(
        r"\{[a-z_][\w\[\]'\"\.()]*\}"
        r"|\$\{"
        r"|['\"]\s*\+\s*[\w$]"
    )

    offenders = []
    for path in _js_sources():
        text = path.read_text()
        for match in handler.finditer(text):
            body = match.group(2)
            if interpolated.search(body):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path}:{line}")
    assert not offenders, "values interpolated into JavaScript: " + ", ".join(offenders)


@pytest.mark.parametrize(
    "source,unsafe",
    [
        ("""html += '<button onclick="' + fnName + '(1)">Next</button>';""", True),
        ('''f"<button onclick=\\"viewAgent('{a['id']}')\\">View</button>"''', True),
        (
            """'<button onclick="copy(' + JSON.stringify(k) + ', this)">Copy</button>'""",
            True,
        ),
        ("""`<button onclick="retract('${r.id}')">x</button>`""", True),
        ('<button\n   onclick="doThing(\'{value}\')">Go</button>', True),
        (
            """'<button onclick="detail(&apos;' + escapeHtml(e.id) + '&apos;)">V</button>'""",
            True,
        ),
        ("""f'<button data-agent-view="{escape_html(a["id"])}">View</button>'""", False),
        ("""<button onclick="closeModal('my-modal')">Close</button>""", False),
        ("""f"<button data-label='{escape_html(x)}' onclick='go()'>Go</button>\"""", False),
    ],
)
def test_the_detector_recognises_every_form_seen_so_far(source, unsafe):
    """The audit is only worth what it detects.

    Each of these is a shape that reached the codebase at some point, plus the
    safe shapes that must not be flagged — a detector that cries wolf gets
    weakened rather than obeyed.
    """
    import re

    handler = re.compile(r"""(?<![\w-])on\w+\s*=\s*\\?(["'])((?:(?!\1)[^>])*)""")
    interpolated = re.compile(
        r"\{[a-z_][\w\[\]'\"\.()]*\}|\$\{|['\"]\s*\+\s*[\w$]"
    )
    flagged = any(interpolated.search(m.group(2)) for m in handler.finditer(source))
    assert flagged is unsafe


def test_the_connector_directory_carries_remote_values_as_data(test_client, admin_token):
    """Directory entries come from a remote index — the least trusted input here."""
    import pathlib

    source = pathlib.Path("app/dashboard/static/js/connectors-directory.js").read_text()
    assert "data-directory-detail=" in source
    assert "data-directory-import=" in source
    assert "showDirectoryDetail(&apos;" not in source
    assert "startDirectoryImport(&apos;" not in source


def test_remote_links_are_restricted_to_web_schemes():
    """`javascript:` in an href runs on click, and the directory is remote."""
    import pathlib

    source = pathlib.Path("app/dashboard/static/js/connectors-directory.js").read_text()
    assert "function safeUrl" in source
    assert "escapeHtml(entry.website)" not in source.split("function safeUrl")[1].split(
        "</a>"
    )[0], "the href must go through safeUrl, not just escaping"
    for field in ("entry.website", "entry.origin_url"):
        assert f"safeUrl({field})" in source


def test_a_webhook_name_is_not_handler_source(test_client, admin_token):
    """The name is operator-supplied text rendered next to its own buttons."""
    r = test_client.post(
        "/api/webhooks",
        headers=_admin(admin_token),
        json={
            "name": "');globalThis.pwned=1;//",
            "url": "https://example.com/hook",
            "event_types": ["activity_created"],
            "secret": "s" * 24,
        },
    )
    assert r.status_code in (200, 201), r.json()

    page = test_client.get("/webhooks", headers=_admin(admin_token)).text
    # The name may appear as escaped text; what it must not do is land in a
    # handler attribute, where entity decoding turns it back into source.
    assert "viewDeliveries('" not in page
    assert "deleteWebhook('" not in page
    assert "data-webhook-name=" in page


def test_imported_connector_names_are_carried_as_data(test_client, admin_token):
    import pathlib

    source = pathlib.Path("app/routes/connectors_page.py").read_text()
    assert "data-actions-type-id=" in source
    assert "onclick=\\'viewActions(" not in source
