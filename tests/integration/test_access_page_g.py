"""The Access page: the third tab under Credentials, what it shows, and who may open it.

The page itself holds no access data: the browser asks for the overview
(``GET /api/access/overview``) and builds the table and the Unused list from the answer. These tests
check the page an admin sees - the heading, the intro, the filters, the Unused groups and the one row
of tabs - and that the page's script parses.
"""

import re
import shutil
import subprocess

import pytest

from app.services.auth_service import create_session, create_user


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


@pytest.fixture
def plain_user(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("reader", "reader@test.local", "testpassword123", "Reader", "user")
    test_client.cookies.set("session_token", create_session("reader", channel="dashboard")["session_id"])
    return test_client


def _tab_labels(html):
    """The labels on the one row of tabs a page shows."""
    row = html.split('<nav class="page-tabs"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r">([^<]+)</a>", row)


def _filter_labels(html):
    """The label of every filter on the page, in the order they are shown."""
    return re.findall(r'<label for="([^"]+)">([^<]+)</label>', html)


def test_the_access_page_says_what_it_lists(admin):
    html = admin.get("/access").text
    assert "<h1>Access</h1>" in html
    assert "Every way a credential, a capability or an agent reaches something, and when it was last used." in html
    for heading in ("Who or what", "Reaches", "How", "Detail", "Last used"):
        assert f"<th>{heading}</th>" in html, heading


def test_the_page_offers_the_four_filters(admin):
    html = admin.get("/access").text
    assert _filter_labels(html) == [
        ("access-show", "Show"),
        ("access-filter-name", "Filter by name"),
        ("access-filter-agent", "Agent"),
        ("access-filter-capability", "Capability"),
        ("access-filter-service", "Service"),
    ]


def test_the_unused_section_names_the_four_groups(admin):
    html = admin.get("/access").text
    assert "<h2>Unused</h2>" in html
    assert "Things nothing uses. Check each before you delete it." in html
    for heading in (
        "Credentials nothing uses",
        "Capabilities nobody can use",
        "Services that look idle",
        "Left behind by a retired agent or service",
    ):
        assert heading in html, heading
    # Nothing is offered to delete here: the list is there to read.
    assert "Delete" not in html


def test_the_page_says_when_the_filters_hide_everything(admin):
    html = admin.get("/access").text
    assert "Nothing matches these filters." in html
    assert "No access is set up yet. Start with a credential on the Credentials tab." in html
    assert "Nothing is unused." in html


def test_access_is_the_third_tab_under_credentials(admin):
    for route in ("/credentials", "/services", "/access"):
        assert _tab_labels(admin.get(route).text) == ["Credentials", "Services", "Access"], route
    html = admin.get("/access").text
    assert '<a href="/access" class="page-tab active" aria-current="page">Access</a>' in html
    assert '<a href="/credentials" class="page-tab"' in html


def test_a_signed_out_person_is_sent_to_the_login_page(admin, test_client):
    test_client.cookies.clear()
    r = test_client.get("/access", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_a_person_who_is_not_an_admin_is_told_the_page_is_for_admins(plain_user):
    r = plain_user.get("/access")
    assert r.status_code == 403
    assert "Admin Access Required" in r.text
    assert "Back to Needs you" in r.text


def test_the_page_reads_the_access_overview_and_shows_no_name_of_its_own(admin):
    """The page carries no access data: the browser builds it from the answer."""
    js = admin.get("/static/js/access_page.js")
    assert js.status_code == 200
    source = js.text
    assert "/api/access/overview" in source
    assert "textContent" in source
    assert "innerHTML" not in source


def test_both_cards_say_loading_until_the_answer_arrives_and_the_error_replaces_it(admin):
    html = admin.get("/access").text
    assert 'id="access-loading"' in html and 'id="access-unused-loading"' in html
    assert html.count("Loading...") == 2
    source = admin.get("/static/js/access_page.js").text
    assert "access-loading" in source and "access-unused-loading" in source
    assert "The access list could not be loaded" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_parses():
    result = subprocess.run(
        ["node", "--check", "app/dashboard/static/js/access_page.js"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
