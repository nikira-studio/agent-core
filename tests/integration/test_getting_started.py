"""Getting started on Needs you: open while a required step is undone, one quiet line once set up, gone when done."""

import re

import pytest

from app.database import get_db
from app.services import activity_service, agent_service, auth_service, connector_service, workspace_service
from tests.conftest import csrf_headers


@pytest.fixture
def authenticated_client(test_client, clean_db):
    auth_service.create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", auth_service.create_session("admin", channel="dashboard")["session_id"])
    return test_client


def _owner():
    with get_db() as conn:
        return conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]


def _set_up(owner):
    workspace_service.create_workspace("ws-done", "ws-done", owner)
    agent_service.create_agent(agent_id="gs-agent", display_name="GS Agent", owner_user_id=owner,
                               read_scopes=["workspace:ws-done"], write_scopes=["workspace:ws-done"])
    activity_service.create_activity(agent_id="gs-agent", user_id=owner, task_description="Did something",
                                     memory_scope="workspace:ws-done")


def _bind_a_service(owner):
    connector_service.create_connector_type(connector_type_id="gs-svc", display_name="GS", auth_type="none",
                                            provider_type="builtin", supported_actions=[])
    return connector_service.create_binding("gs-svc", "GS connection", f"user:{owner}")


def test_a_required_step_undone_shows_the_open_card_below_anything_waiting(authenticated_client):
    html = authenticated_client.get("/").text
    assert '<div class="card" id="overview-getting-started"><h2>Getting started</h2>' in html
    assert "getting-started-fold" not in html
    # never above real asks
    assert html.index('id="overview-getting-started"') > html.index("Needs you</h1>")
    attention = html.find('id="overview-attention"')
    if attention != -1:
        assert attention < html.index('id="overview-getting-started"')


def test_once_the_required_steps_are_done_it_is_one_quiet_line_at_the_bottom(authenticated_client):
    _set_up(_owner())
    html = authenticated_client.get("/").text
    assert '<div class="card" id="overview-getting-started">' not in html
    fold = re.search(r'<details class="getting-started-fold" id="overview-getting-started"><summary>(.*?)</summary>', html)
    assert fold and fold.group(1) == "Getting started: set up · 2 optional steps left"
    assert "data-hide-getting-started" in html and "Settings, under Account" in html
    assert html.index("getting-started-fold") > html.index("Recent Activity")  # at the bottom, after the real content


def test_hiding_it_is_for_good_per_user_and_settings_brings_it_back(authenticated_client):
    _set_up(_owner())
    hide = authenticated_client.post("/api/dashboard/user-settings", json={"getting_started_hidden": True},
                                     headers=csrf_headers(authenticated_client))
    assert hide.status_code == 200, hide.text
    assert 'id="overview-getting-started"' not in authenticated_client.get("/").text
    account = authenticated_client.get("/settings").text
    assert 'id="user-getting-started"' in account and 'id="user-getting-started" checked' not in account
    show = authenticated_client.post("/api/dashboard/user-settings", json={"getting_started_hidden": False},
                                     headers=csrf_headers(authenticated_client))
    assert show.status_code == 200
    assert "getting-started-fold" in authenticated_client.get("/").text
    # saving a timezone alone never touches the hidden flag, and the flag alone never clears the timezone
    authenticated_client.post("/api/dashboard/user-settings", json={"getting_started_hidden": True}, headers=csrf_headers(authenticated_client))
    authenticated_client.post("/api/dashboard/user-settings", json={"timezone": "UTC"}, headers=csrf_headers(authenticated_client))
    assert 'id="overview-getting-started"' not in authenticated_client.get("/").text


def test_when_every_step_is_done_it_disappears_by_itself(authenticated_client, monkeypatch):
    owner = _owner()
    _set_up(owner)
    _bind_a_service(owner)
    monkeypatch.setattr(auth_service, "is_otp_enrolled", lambda user_id: True)
    assert 'id="overview-getting-started"' not in authenticated_client.get("/").text


def test_give_it_a_service_counts_any_enabled_connection(authenticated_client):
    owner = _owner()
    html = authenticated_client.get("/").text
    step = re.search(r"<li( style='opacity:\.6')?><a href='/connectors'>Give it a service", html)
    assert step and step.group(1) is None  # nothing bound: not done
    binding = _bind_a_service(owner)
    html = authenticated_client.get("/").text
    step = re.search(r"<li( style='opacity:\.6')?><a href='/connectors'>Give it a service", html)
    assert step and step.group(1)  # an enabled connection exists: done
    connector_service.update_binding(binding["id"], enabled=False)
    step = re.search(r"<li( style='opacity:\.6')?><a href='/connectors'>Give it a service", authenticated_client.get("/").text)
    assert step and step.group(1) is None  # a disabled one does not count


def test_an_ask_in_an_agents_private_memory_uses_plain_words_on_needs_you(authenticated_client):
    owner = _owner()
    agent_service.create_agent(agent_id="it-admin", display_name="IT Admin", owner_user_id=owner,
                               read_scopes=["agent:it-admin"], write_scopes=["agent:it-admin"])
    task = activity_service.create_activity(agent_id="it-admin", user_id=owner, task_description="Approve the notes",
                                           memory_scope="agent:it-admin")
    activity_service.set_handoff(task["id"], {"needs_human": "Please approve the notes."})
    activity_service.update_activity(task["id"], status="blocked", task_result="Waiting")
    html = authenticated_client.get("/").text
    row = html.split("Approve the notes", 1)[1][:1500]
    assert "IT Admin&#39;s own memory" in row
    assert "agent:it-admin" not in row
