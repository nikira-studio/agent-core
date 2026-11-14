"""A connection check has three outcomes (passed, failed, can't be tested) and says what to do about a changed tool list."""

import json
import re

import pytest

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.database import get_db
from app.services import connector_service as cs
from tests.conftest import csrf_headers


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    session = create_session("admin", channel="dashboard")
    test_client.cookies.set("session_token", session["session_id"])
    return test_client


def _engine(actions, requests, *, test_action=None, disabled=()):
    spec = {"requests": requests}
    if test_action:
        spec["test_action"] = test_action
    return HttpEngine(
        {
            "supported_actions": actions,
            "disabled_actions": list(disabled),
            "backend_json": json.dumps(spec),
        }
    )


def _read(name, required=(), side_effect="read"):
    return {
        "name": name,
        "side_effect": side_effect,
        "input_schema": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in required},
            "required": list(required),
        },
    }


# ---- how the test action is chosen -------------------------------------------------------------------------

def test_the_adapters_named_test_action_is_used_when_it_needs_nothing_we_lack():
    engine = _engine([_read("task_status"), _read("list_things")],
                     {"task_status": {"method": "GET"}, "list_things": {"method": "GET"}}, test_action="task_status")
    assert engine.pick_test_action({}) == "task_status"


def test_a_named_action_that_needs_a_value_we_do_not_have_is_skipped_for_one_that_needs_none():
    engine = _engine([_read("task_status", required=["task_id"]), _read("list_tasks")],
                     {"task_status": {"method": "GET"}, "list_tasks": {"method": "GET"}}, test_action="task_status")
    assert engine.pick_test_action({}) == "list_tasks"
    # ...but with the value among the connection's defaults, the named action is the one
    assert engine.pick_test_action({"task_id": "t-1"}) == "task_status"


def test_only_actions_that_need_input_means_cannot_test_and_nothing_is_sent(monkeypatch):
    engine = _engine([_read("task_status", required=["task_id"])], {"task_status": {"method": "GET"}},
                     test_action="task_status")
    sent = []
    monkeypatch.setattr(engine, "execute", lambda *a, **k: sent.append(a) or {"success": True})
    result = engine.test_connection(Credential(None), "{}")
    assert result["untestable"] and not result["success"] and result["error_code"] == "NOT_TESTABLE"
    assert "no read-only action to try" in result["note"] and "error" not in result
    assert sent == []


@pytest.mark.parametrize("side_effect,method", [("write", "POST"), ("destructive", "DELETE"), (None, "POST")])
def test_write_actions_are_never_picked(side_effect, method, monkeypatch):
    action = _read("do_it", side_effect=side_effect)
    engine = _engine([action], {"do_it": {"method": method}}, test_action="do_it")
    sent = []
    monkeypatch.setattr(engine, "execute", lambda *a, **k: sent.append(a) or {"success": True})
    assert engine.test_connection(Credential(None), "{}")["untestable"] is True
    assert sent == []


def test_a_disabled_test_action_falls_through_and_conventional_names_come_first():
    engine = _engine([_read("task_status"), _read("ping"), _read("list_x")],
                     {"task_status": {"method": "GET"}, "ping": {"method": "GET"}, "list_x": {"method": "GET"}},
                     test_action="task_status", disabled=["task_status"])
    assert engine.pick_test_action({}) == "ping"


def test_a_get_request_with_no_declared_side_effect_counts_as_read_only():
    engine = _engine([{"name": "list_x", "input_schema": {"type": "object"}}], {"list_x": {"method": "GET"}})
    assert engine.pick_test_action({}) == "list_x"


# ---- what is stored and shown -------------------------------------------------------------------------------

class _Exec:
    def __init__(self, result):
        self.result = result

    def test_connection(self, cred, config):
        return self.result


@pytest.fixture
def one_connection(clean_db, monkeypatch):
    cs.create_connector_type("plain-svc", "Plain service", provider_type="builtin", auth_type="none")
    binding = cs.create_binding("plain-svc", "Plain connection", "user:someone")
    return binding


def _stored(binding_id):
    b = cs.get_binding(binding_id)
    return b["last_tested_at"], b["last_error"], b["last_test_note"]


def test_cannot_test_records_a_note_and_no_error_then_a_pass_clears_it(one_connection, monkeypatch):
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec(
        {"success": False, "untestable": True, "error_code": "NOT_TESTABLE", "note": HttpEngine.UNTESTABLE_NOTE}))
    result = cs.test_binding(one_connection["id"])
    assert result["untestable"]
    tested, error, note = _stored(one_connection["id"])
    assert tested and error is None and note == HttpEngine.UNTESTABLE_NOTE

    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec({"success": True}))
    cs.test_binding(one_connection["id"])
    assert _stored(one_connection["id"])[1:] == (None, None)

    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec({"success": False, "error": "refused"}))
    cs.test_binding(one_connection["id"])
    assert _stored(one_connection["id"])[1:] == ("refused", None)


def test_the_capability_summary_calls_it_cannot_test_not_failed(one_connection, monkeypatch):
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec({"success": False, "untestable": True, "note": "n"}))
    cs.test_binding(one_connection["id"])
    b = cs.get_binding(one_connection["id"])
    status = "failed" if b["last_error"] else ("cannot_test" if b["last_test_note"] else "passed")
    assert status == "cannot_test"


def test_health_check_counts_three_outcomes_and_names_who_can_use_each(admin_client, monkeypatch):
    cs.create_connector_type("svc-ok", "OK service", provider_type="builtin", auth_type="none")
    cs.create_connector_type("svc-skip", "Skip service", provider_type="builtin", auth_type="none")
    cs.create_connector_type("svc-bad", "Bad service", provider_type="builtin", auth_type="none")
    with get_db() as conn:
        me = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    cs.create_binding("svc-ok", "ok one", f"user:{me}")
    cs.create_binding("svc-skip", "skip one", f"user:{me}")
    cs.create_binding("svc-bad", "bad one", f"user:{me}")
    answers = {
        "svc-ok": {"success": True},
        "svc-skip": {"success": False, "untestable": True, "error_code": "NOT_TESTABLE", "note": HttpEngine.UNTESTABLE_NOTE},
        "svc-bad": {"success": False, "error": "refused"},
    }
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec(answers[ct["id"]]))
    r = admin_client.post("/api/connector-types/health-check", json={}, headers=csrf_headers(admin_client))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["passed"], data["failed"], data["cannot_test"], data["total"]) == (1, 1, 1, 3)
    by_name = {x["binding_name"]: x for x in data["results"]}
    assert by_name["skip one"]["state"] == "cannot_test" and by_name["skip one"]["error"] is None
    assert by_name["ok one"]["scope_label"] == "You"  # the person asking: not "user:abc123"
    assert by_name["bad one"]["state"] == "failed" and by_name["bad one"]["error"] == "refused"


def test_the_services_page_shows_cannot_test_neutrally_and_the_last_check(admin_client, monkeypatch):
    cs.create_connector_type("svc-skip", "Skip service", provider_type="builtin", auth_type="none")
    with get_db() as conn:
        me = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    cs.create_binding("svc-skip", "skip one", f"user:{me}")
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec(
        {"success": False, "untestable": True, "error_code": "NOT_TESTABLE", "note": HttpEngine.UNTESTABLE_NOTE}))
    admin_client.post("/api/connector-types/health-check", json={}, headers=csrf_headers(admin_client))
    page = admin_client.get("/connectors").text
    card = page.split("Skip service", 1)[1].split("connector-card-actions", 1)[0]
    assert "Can&#39;t be tested" in card or "Can't be tested" in card
    assert "issue(s)" not in card and "untested" not in card and "badge-danger" not in card
    assert re.search(r"Last check \d{4}-\d{2}-\d{2}: 1 can't be tested", card)
    import html as _html

    assert "status-neutral" in page and "Can't be tested (" in _html.unescape(page)  # the connection row, not red


# ---- changed tool list --------------------------------------------------------------------------------------

def _tool(name, props, required=(), read_only=True):
    return {"name": name, "inputSchema": {"type": "object", "properties": {p: {"type": "string"} for p in props},
                                          "required": list(required)},
            "annotations": {"readOnlyHint": read_only}}


def test_tool_changes_say_what_differs_in_plain_terms():
    stored = [_tool("query-docs", ["libraryId", "query"], ["libraryId", "query"]), _tool("resolve", ["name"])]
    now = [_tool("query-docs", ["libraryId", "query", "language"], ["libraryId", "query"]), _tool("resolve", ["name"]),
           _tool("brand-new", [])]
    changes = cs.mcp_tool_changes(stored, now)
    assert changes["added"] == ["brand-new"] and changes["removed"] == []
    assert changes["changed"] == [{"name": "query-docs", "inputs_added": ["language"], "inputs_removed": [],
                                   "newly_required": [], "other": False}]
    text = cs.describe_tool_changes(changes)
    assert "new: brand-new" in text and "query-docs (new input language)" in text
    assert cs.mcp_tool_changes(stored, stored) == {"added": [], "removed": [], "changed": []}


def _mcp_setup(snapshot_tools):
    snapshot = json.dumps({"server_info": {}, "capabilities": {}, "tools": snapshot_tools})
    cs.create_connector_type("mcp-svc", "Docs service", provider_type="mcp", auth_type="none",
                             endpoint_url="https://mcp.example.com/mcp", transport_type="streamable_http",
                             tool_snapshot_json=snapshot, supported_actions=[t["name"] for t in snapshot_tools])
    return cs.create_binding("mcp-svc", "Docs connection", "user:someone")


def test_a_changed_tool_list_fails_with_a_plain_message_and_the_action_to_take(clean_db, monkeypatch):
    stored = [_tool("query-docs", ["libraryId", "query"], ["libraryId", "query"])]
    binding = _mcp_setup(stored)
    live = [_tool("query-docs", ["libraryId", "query", "language"], ["libraryId", "query"])]
    monkeypatch.setattr(cs, "discover_mcp_binding_tools", lambda *a, **k: live)
    result = cs.test_binding(binding["id"])
    assert not result["success"] and result["error_code"] == "TOOLS_CHANGED"
    assert result["error"].startswith("This service's tool list has changed since it was added.")
    assert "query-docs (new input language)" in result["error"] and "Update tool list" in result["error"]
    assert "((" not in result["error"]
    assert "register a separate connector type" not in result["error"]
    assert cs.get_binding(binding["id"])["last_error"] == result["error"]


def test_update_tool_list_previews_then_saves_only_on_a_second_step(admin_client, monkeypatch):
    from app.services import mcp_provider_service as mp

    stored = [_tool("query-docs", ["libraryId", "query"], ["libraryId", "query"])]
    binding = _mcp_setup(stored)
    live = [_tool("query-docs", ["libraryId", "query", "language"], ["libraryId", "query"]), _tool("extra", [])]
    monkeypatch.setattr(cs, "discover_mcp_binding_tools", lambda *a, **k: live)
    monkeypatch.setattr(cs, "_mcp_binding_request_config", lambda b, ct, cred=None: ("https://mcp.example.com/mcp", {}, 5000))
    monkeypatch.setattr(mp, "discover_mcp_server", lambda url, timeout_ms=0, headers=None: mp.MCPDiscoveryResult(
        server_name="docs", protocol_version="2025-03-26", capabilities={}, tools=live))
    cs.test_binding(binding["id"])  # flags the connection

    before = cs.get_connector_type("mcp-svc")["tool_snapshot_json"]
    preview = admin_client.post("/api/connector-types/mcp-svc/tool-changes", json={}, headers=csrf_headers(admin_client))
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert data["changes"]["added"] == ["extra"] and not data["unchanged"]
    assert cs.get_connector_type("mcp-svc")["tool_snapshot_json"] == before  # a preview saves nothing

    saved = admin_client.post("/api/connector-types/mcp-svc/refresh", json={"binding_id": binding["id"]},
                              headers=csrf_headers(admin_client))
    assert saved.status_code == 200, saved.text
    assert {t["name"] for t in json.loads(cs.get_connector_type("mcp-svc")["tool_snapshot_json"])["tools"]} == {"query-docs", "extra"}
    assert cs.get_binding(binding["id"])["last_error"] is None  # no longer flagged
    assert cs.test_binding(binding["id"])["success"]


def test_the_service_card_offers_update_tool_list_for_an_mcp_service_with_a_connection(admin_client):
    _mcp_setup([_tool("query-docs", ["q"])])
    page = admin_client.get("/connectors").text
    assert 'data-tools-update-card="mcp-svc"' in page and "Update tool list" in page and 'id="tool-update-modal"' in page


def test_a_failed_check_with_no_message_still_says_something(one_connection, monkeypatch):
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec({"success": False, "status": 500}))
    result = cs.test_binding(one_connection["id"])
    assert result["error"] == "The service answered with an error (HTTP 500)."
    assert cs.get_binding(one_connection["id"])["last_error"] == result["error"]


def test_a_passing_check_does_not_hand_back_the_start_of_the_answer(one_connection, monkeypatch):
    """The first bytes of a service's answer can be private (chat names, message ids); the check says only that it passed."""
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _Exec(
        {"success": True, "status": 200, "body_preview": '{"chats":[{"chat_name":"Private person"}]}'}))
    result = cs.test_binding(one_connection["id"])
    assert result["success"] and result["status"] == 200 and "body_preview" not in result
