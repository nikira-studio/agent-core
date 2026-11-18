"""Release B part 1: the dashboard's three words.

- capability: something an agent can do through Agent Core (the old "service" of the catalogue)
- service: a program or container that only fetches secrets
- permission: an agent's on/off flags (the old "capabilities" on the Agents page)

These tests check the words a person reads, not the behaviour behind them.
"""

import re

import pytest


@pytest.fixture
def authenticated_client(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


def sidebar_entries(html):
    """The sidebar as (href, label) pairs, in the order they are shown."""
    nav = html.split('<nav aria-label="Main" id="main-nav">', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'<a href="([^"]+)" class="[^"]*"[^>]*><span>([^<]+)</span>', nav)


def tab_labels(html):
    """The labels on the one row of tabs a page shows."""
    if 'class="page-tabs"' not in html:
        return []
    row = html.split('<nav class="page-tabs"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r">([^<]+)</a>", row)


def test_the_sidebar_names_the_capabilities_page_and_keeps_the_seven_entries(authenticated_client):
    entries = sidebar_entries(authenticated_client.get("/").text)
    assert [label for _, label in entries] == [
        "Needs you", "Activity", "Memory", "Agents", "Capabilities", "Credentials", "Settings",
    ]
    assert [href for href, _ in entries] == [
        "/", "/activity", "/memory", "/agents", "/connectors", "/credentials", "/settings",
    ]
    assert "Services" not in [label for _, label in entries]


def test_each_section_shows_its_own_row_of_tabs(authenticated_client):
    assert tab_labels(authenticated_client.get("/credentials").text) == ["Credentials", "Services"]
    assert tab_labels(authenticated_client.get("/services").text) == ["Credentials", "Services"]
    assert tab_labels(authenticated_client.get("/agents").text) == ["Agents", "Workspaces", "Connect"]
    assert tab_labels(authenticated_client.get("/connectors").text) == []


def test_the_capabilities_page_says_what_it_used_to_be_called(authenticated_client):
    html = authenticated_client.get("/connectors").text
    assert "Previously called Services" in html
    # The note is marked so it can go after one release.
    assert "<!-- remove after one release -->" in html


def test_the_old_services_address_points_at_the_services_page(authenticated_client):
    r = authenticated_client.get("/agents?show=services", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/services"


def test_the_agents_page_calls_the_flags_permissions(authenticated_client):
    html = authenticated_client.get("/agents").text
    for old in ("Run service actions", "See services", "Read credential values", "Credential references", "Service agent"):
        assert old not in html, old
    for new in ("Run capabilities", "See capabilities", "Fetch secrets", "Reference credentials"):
        assert new in html, new


def test_the_services_page_is_about_the_programs_that_fetch_secrets(authenticated_client):
    html = authenticated_client.get("/services").text
    assert "Programs and containers that fetch secrets" in html
    assert "+ Create service" in html


def test_no_page_shows_the_old_wording(authenticated_client):
    old_words = (
        "Run service actions",
        "See services",
        "Ready-made services",
        "Service directory",
        "Services help",
    )
    for route in ("/connectors", "/credentials", "/agents", "/", "/settings"):
        html = authenticated_client.get(route).text
        for old in old_words:
            assert old not in html, f"{route} still shows {old}"
