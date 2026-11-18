"""Release D, the dialogs: a connection's "Who can use it" is a list a person edits, and an agent's Access dialog can
add a capability.

The names here are made up for the tests. The pages are checked for the words a person reads, and the server is asked
the same questions the dialogs ask, so what the dialog shows is what the server answers.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from app.services import agent_service, connector_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

REPO = pathlib.Path(__file__).resolve().parents[2]
JS_FILE = REPO / "app" / "dashboard" / "static" / "js" / "agent_access.js"
CONNECTORS_PAGE = REPO / "app" / "routes" / "connectors_page.py"
AGENTS_PAGE = REPO / "app" / "routes" / "agents_page.py"


@pytest.fixture
def world(test_client, clean_db):
    """An admin at the dashboard, a person, two agents that may use the connection, one that may not, and a service."""
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("alex", "alex@test.local", "testpassword123", "Alex", "user")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("team", "Team Place", "admin")
    workspace_service.create_workspace("desk", "Desk Place", "admin")
    agent_service.create_agent(
        "agent-a", "Agent A", "admin",
        capabilities=["memory", "connectors_read", "connectors_execute"],
        read_scopes=["workspace:team"], write_scopes=[],
    )
    agent_service.create_agent(
        "agent-b", "Agent B", "admin",
        capabilities=["memory", "connectors_read", "connectors_execute"],
        read_scopes=["workspace:team"], write_scopes=[],
    )
    # Agent C may see the capability but not run it: it needs the Run capabilities permission.
    agent_service.create_agent(
        "agent-c", "Agent C", "admin",
        capabilities=["memory", "connectors_read"],
        read_scopes=[], write_scopes=[],
    )
    agent_service.create_agent("fetcher", "Fetcher", "admin", kind="service")
    connector_service.create_connector_type(
        connector_type_id="ledger-cap", display_name="Ledger capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    binding = connector_service.create_binding(
        "ledger-cap", "Ledger connection", "workspace:team",
        config_json='{"base_url": "http://example.test"}',
    )
    return {"client": test_client, "binding": binding, "headers": csrf_headers(test_client)}


def _connection_data(world):
    return world["client"].get(f"/api/access/connections/{world['binding']['id']}").json()["data"]


def _entries(data):
    return [{"kind": e["kind"], "ref": e["ref"], "exclude": e["exclude"]} for e in data["audience"]]


# ── the pages offer the new dialogs ──────────────────────────────────────────


def test_the_connection_dialog_lists_who_can_use_it_and_lets_a_person_edit_it(world):
    html = world["client"].get("/connectors").text
    assert "Who can use it" in html
    assert 'id="access-connection-allowed"' in html
    assert 'id="access-connection-remove-preview"' in html
    assert 'id="access-connection-add-kind"' in html
    assert 'id="access-connection-add-ref"' in html
    assert 'id="access-connection-add"' in html
    assert 'id="access-connection-add-preview"' in html
    assert 'id="access-connection-users"' in html
    assert "Using it now" in html


def test_the_four_ways_to_name_someone_are_offered_in_plain_words(world):
    html = world["client"].get("/connectors").text
    assert '<option value="agent">An agent</option>' in html
    assert '<option value="workspace">A workspace</option>' in html
    assert '<option value="person_agents">All of one person\'s agents</option>' in html
    assert '<option value="agent_exclude">Exclude an agent</option>' in html


def test_the_fixed_sentence_about_what_adding_gives_is_there(world):
    html = world["client"].get("/connectors").text
    assert (
        "Adding someone lets them use this connection. "
        "It does not give them memory or credential access."
    ) in html


def test_the_agent_dialog_can_add_a_capability(world):
    html = world["client"].get("/agents").text
    assert "Add a capability" in html
    assert 'id="access-agent-capability-pick"' in html
    assert 'id="access-agent-capability-add"' in html
    assert 'id="access-agent-capability-preview"' in html


# ── the words the dialogs show live in the script ───────────────────────────


def test_the_script_keeps_the_words_the_dialogs_show():
    script = JS_FILE.read_text()
    for phrase in (
        "Nobody can use this connection yet.",
        "Agents that gain it: ",
        "Agents that lose it: ",
        "This agent can already use every connection.",
        "A new agent of that person starts excluded; you can include it here.",
        "No agent can use it right now.",
    ):
        assert phrase in script, phrase


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_access_script_parses():
    result = subprocess.run(["node", "--check", str(JS_FILE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _script_texts(script: str) -> list[str]:
    """The words a person reads: the string literals in the script that read as text, not as ids."""
    without_comments = re.sub(r"(?m)^\s*//.*$", "", script)
    texts = [a or b for a, b in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", without_comments)]
    return [t for t in texts if " " in t and re.search(r"[A-Za-z]", t)]


def _connection_dialog_markup() -> str:
    source = CONNECTORS_PAGE.read_text()
    start = source.index("<!-- Access Modal: who can use one connection -->")
    return source[start:source.index('\n    """', start)]


def _agent_dialog_markup() -> str:
    source = AGENTS_PAGE.read_text()
    start = source.index('agent_access_dialog = """')
    return source[start:source.index('"""', start + 24)]


def _visible_text(markup: str) -> str:
    """The words a person reads: the markup with the tags (and so their attributes) taken out."""
    return re.sub(r"<[^>]*>", " ", markup)


def test_no_word_a_person_does_not_know_appears_in_the_new_dialogs():
    """Nobody reading these dialogs should have to know what a binding, a grant, a scope or an audience is."""
    for text in _script_texts(JS_FILE.read_text()):
        assert not re.search(r"\baudience\b", text, re.IGNORECASE), text
        assert not re.search(r"\bscope\b", text, re.IGNORECASE), text
    for markup in (_connection_dialog_markup(), _agent_dialog_markup()):
        shown = _visible_text(markup)
        assert not re.search(r"\baudience\b", shown, re.IGNORECASE), shown
        assert not re.search(r"\bscope\b", shown, re.IGNORECASE), shown


# ── the list a person edits ──────────────────────────────────────────────────


def test_the_dialog_is_given_the_list_it_edits_and_the_agents_who_use_it(world):
    data = _connection_data(world)
    assert [e["text"] for e in data["audience"]] == ["Everyone in workspace Team Place"]
    assert {u["agent_id"] for u in data["users"]} == {"agent-a", "agent-b"}
    assert data["users"][0]["reason"]
    # The names the pick offers: agents (never services), workspaces and people.
    assert {a["id"] for a in data["audience_options"]["agents"]} == {"agent-a", "agent-b", "agent-c"}
    assert {w["id"] for w in data["audience_options"]["workspaces"]} == {"team", "desk"}
    assert {p["id"] for p in data["audience_options"]["people"]} == {"admin", "alex"}


def test_taking_one_off_the_list_shows_who_loses_it_before_anything_is_saved(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    preview = client.post(
        "/api/access/preview/audience",
        json={"binding_id": binding["id"], "audience": []},
        headers=headers,
    ).json()["data"]
    assert [who["display_name"] for who in preview["lose"]] == ["Agent A", "Agent B"]
    assert preview["gain"] == []
    # Nothing is saved until the person presses Remove.
    assert len(_entries(_connection_data(world))) == 1


def test_putting_one_on_the_list_saves_it_and_the_users_follow(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    added = client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "agent", "ref": "agent-c"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    data = added.json()["data"]
    assert {e["ref"] for e in data["audience"]} == {"team", "agent-c"}
    by_agent = {u["agent_id"]: u for u in data["users"]}
    assert by_agent["agent-c"]["status"] == "needs_permission"
    assert by_agent["agent-c"]["needs_label"] == "Run capabilities"


def test_removing_one_takes_it_off_the_list(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "agent", "ref": "agent-c"},
        headers=headers,
    )
    removed = client.delete(
        f"/api/access/connections/{binding['id']}/audience/agent/agent-c", headers=headers
    )
    assert removed.status_code == 200, removed.text
    assert [e["ref"] for e in removed.json()["data"]["audience"]] == ["team"]
    assert "agent-c" not in {u["agent_id"] for u in removed.json()["data"]["users"]}


def test_a_persons_agents_can_be_named_all_at_once(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    added = client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "person_agents", "ref": "alex"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    assert "All of Alex's agents" in [e["text"] for e in added.json()["data"]["audience"]]


def test_an_excluded_agent_is_offered_and_adding_it_turns_the_exclusion_into_a_named_entry(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    put = client.put(
        f"/api/access/connections/{binding['id']}/audience",
        json={"audience": [
            {"kind": "workspace", "ref": "team"},
            {"kind": "agent", "ref": "agent-b", "exclude": True},
        ]},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    data = _connection_data(world)
    assert {e["text"] for e in data["audience"]} == {"Everyone in workspace Team Place", "Agent B is excluded"}
    assert "agent-b" not in {u["agent_id"] for u in data["users"]}
    # Adding that agent is what the dialog does: the exclusion becomes a named entry.
    added = client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "agent", "ref": "agent-b"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    entries = {e["ref"]: e for e in added.json()["data"]["audience"]}
    assert entries["agent-b"]["exclude"] is False
    assert "agent-b" in {u["agent_id"] for u in added.json()["data"]["users"]}


def test_a_service_is_never_offered_and_is_refused(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    data = _connection_data(world)
    assert "fetcher" not in {a["id"] for a in data["audience_options"]["agents"]}
    refused = client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "agent", "ref": "fetcher"},
        headers=headers,
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "INVALID_AUDIENCE"
    assert refused.json()["error"]["message"]  # a plain message the dialog can show


# ── the other end: the agent's own dialog ───────────────────────────────────


def test_the_agent_side_adds_itself_and_nobody_else_loses_it(world):
    client, headers = world["client"], world["headers"]
    binding = world["binding"]
    # What the agent's dialog does: read the list, put this agent on it, and ask who would gain it.
    entries = _entries(_connection_data(world))
    entries.append({"kind": "agent", "ref": "agent-c", "exclude": False})
    preview = client.post(
        "/api/access/preview/audience",
        json={"binding_id": binding["id"], "audience": entries},
        headers=headers,
    ).json()["data"]
    assert [g["display_name"] for g in preview["gain"]] == ["Agent C"]
    assert preview["lose"] == []
    saved = client.post(
        f"/api/access/connections/{binding['id']}/audience",
        json={"kind": "agent", "ref": "agent-c"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    summary = client.get("/api/access/agents/agent-c").json()["data"]
    assert [c["name"] for c in summary["connections"]] == ["Ledger connection"]
    assert summary["connections"][0]["status"] == "needs_permission"


def test_the_agent_that_may_already_use_the_connection_is_not_offered_it_again(world):
    client = world["client"]
    binding = world["binding"]
    summary = client.get("/api/access/agents/agent-a").json()["data"]
    assert [c["id"] for c in summary["connections"]] == [binding["id"]]
    bindings = client.get("/api/connector-bindings").json()["data"]["bindings"]
    assert binding["id"] in [b["id"] for b in bindings]
