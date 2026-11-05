"""A plain HTTP service that signs in first, then calls a fixed list of paths."""

import datetime
import hashlib
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.connectors.errors import SessionExpiredError
from app.connectors.generic_http import GenericHttpConnector
from app.services import connector_service, credential_service

USER, PASSWORD = "readonly", "pa:ss:word"  # a colon inside the password must still work


class _Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, cookies=()):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _signed_in(self):
        return f"sid={self.server.sid}" in (self.headers.get("Cookie") or "")

    def do_POST(self):
        self.server.requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/login":
            if body.get("username") == USER and body.get("password") == PASSWORD:
                self.server.logins += 1
                self.server.sid = f"tok{self.server.logins}"
                return self._send(200, {"ok": True}, [f"sid={self.server.sid}; Path=/; HttpOnly", "csrf=zzz; Path=/"])
            return self._send(400, {"error": "bad login"})
        self.server.writes += 1
        self._send(200, {"wrote": True})

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        if not self._signed_in():
            return self._send(401, {"error": "login required"})
        self._send(200, {"path": self.path}, ["tracking=1; Path=/"])


def _serve(tls=None):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    server.sid, server.logins, server.writes, server.requests = "none", 0, 0, []
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def stub(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ALLOWED_INTERNAL_HOSTS", "127.0.0.1")
    server = _serve()
    yield server
    server.shutdown()


def _config(server, scheme="http", **extra):
    config = {
        "base_url": f"{scheme}://127.0.0.1:{server.server_address[1]}",
        "login": {"path": "/api/login", "body": {"username": "{{credential.username}}", "password": "{{credential.password}}"}},
        "credential_split": ":",
        "allowed": ["GET /api/data"],
    }
    config.update(extra)
    return json.dumps(config)


class _Cred:
    def __init__(self, raw):
        self.raw, self.fields = raw, {}


GOOD = _Cred(f"{USER}:{PASSWORD}")


def test_connection_test_signs_in_and_returns_only_ok(stub):
    result = GenericHttpConnector().test_connection(GOOD, _config(stub))
    assert result == {"success": True, "status": "signed_in"}
    assert stub.logins == 1 and "tok1" not in json.dumps(result)


def test_a_wrong_password_fails_plainly_without_echoing_it(stub):
    result = GenericHttpConnector().test_connection(_Cred(f"{USER}:nope"), _config(stub))
    assert result["success"] is False and "HTTP 400" in result["error"]
    assert "nope" not in json.dumps(result) and stub.logins == 0


def test_a_password_with_colons_signs_in(stub):
    connector = GenericHttpConnector()
    session = connector.refresh_session(GOOD, _config(stub), None)["session"]
    assert set(session["cookies"]) == {"sid", "csrf"}


def test_calls_send_the_cookie_but_never_return_it(stub):
    connector = GenericHttpConnector()
    config = _config(stub)
    with pytest.raises(SessionExpiredError):
        connector.execute("GET /api/data", {}, GOOD, config, session=None)
    session = connector.refresh_session(GOOD, config, None)["session"]
    result = connector.execute("GET /api/data", {}, GOOD, config, session=session)
    assert result["success"] and result["body"] == {"path": "/api/data"}
    blob = json.dumps(result).lower()
    assert "set-cookie" not in blob and "tok1" not in blob and "tracking" not in blob


def test_only_the_listed_requests_are_ever_sent(stub):
    connector = GenericHttpConnector()
    config = _config(stub)
    session = connector.refresh_session(GOOD, config, None)["session"]
    before = len(stub.requests)
    for action, params in (
        ("POST /api/write", {}),
        ("GET /api/other", {}),
        ("GET /api/data/../other", {}),
        ("GET /api/data?x=1", {}),
        ("GET /api/data", {"method": "POST"}),
        ("GET /api/data", {"url": "http://127.0.0.1/api/data"}),
        ("call_endpoint", {"method": "DELETE", "path": "/api/data"}),
    ):
        result = connector.execute(action, params, GOOD, config, session=session)
        assert result.get("error_code") == "REQUEST_NOT_ALLOWED", (action, params, result)
    assert len(stub.requests) == before and stub.writes == 0


def test_a_stale_session_asks_for_a_new_sign_in_but_a_fresh_refusal_does_not(stub):
    connector = GenericHttpConnector()
    config = _config(stub)
    session = connector.refresh_session(GOOD, config, None)["session"]
    stub.sid = "rotated"  # the service forgot the session
    stale = dict(session, at=session["at"] - 3600)
    with pytest.raises(SessionExpiredError):
        connector.execute("GET /api/data", {}, GOOD, config, session=stale)
    refused = connector.execute("GET /api/data", {}, GOOD, config, session=session)
    assert refused["success"] is False and "right after signing in" in refused["error"]


def test_the_cookie_is_kept_between_calls_and_renewed_when_the_service_forgets_it(test_client, admin_token, stub):
    cred = credential_service.create_credential("user:admin", "login-test", f"{USER}:{PASSWORD}")
    connector_service.create_connector_type(
        connector_type_id="loginsvc", display_name="Login service", auth_type="none",
        supported_actions=[{"name": "GET /api/data", "side_effect": "read"}],
        provider_type="openapi", backend_type="generic_http",
    )
    binding = connector_service.create_binding(
        "loginsvc", "Login service", "user:admin", credential_id=cred["id"], config_json=_config(stub)
    )
    first = connector_service.execute_binding_action(binding["id"], "GET /api/data", {})
    second = connector_service.execute_binding_action(binding["id"], "GET /api/data", {})
    assert first["success"] and second["success"], (first, second)
    assert stub.logins == 1  # one sign-in served both calls

    stub.sid = "forgotten"  # later on, the service has dropped the session
    from app.services import connector_session_service

    cached = connector_session_service.load_session(binding["id"])
    cached["at"] -= 3600
    connector_session_service.save_session(binding["id"], cached)
    third = connector_service.execute_binding_action(binding["id"], "GET /api/data", {})
    assert third["success"] and stub.logins == 2
    assert "tok" not in json.dumps([first, second, third]).lower()

    connector_service.execute_binding_action_with_logging(binding["id"], "GET /api/data", {})
    from app.database import get_db

    with get_db() as conn:
        logged = json.dumps([dict(r) for r in conn.execute("SELECT * FROM connector_executions")])
    assert "tok" not in logged.lower() and PASSWORD not in logged


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "controller.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return str(cert_path), str(key_path), hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_a_pinned_self_signed_certificate_is_accepted_and_a_changed_one_is_not(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ALLOWED_INTERNAL_HOSTS", "127.0.0.1")
    cert, key, fingerprint = _self_signed(tmp_path)
    server = _serve(tls=(cert, key))
    try:
        connector = GenericHttpConnector()
        unpinned = connector.test_connection(GOOD, _config(server, scheme="https"))
        assert unpinned["success"] is False and server.logins == 0  # normal checking still refuses it

        pinned = _config(server, scheme="https", tls={"pin_sha256": ":".join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()})
        assert connector.test_connection(GOOD, pinned) == {"success": True, "status": "signed_in"}

        changed = _config(server, scheme="https", tls={"pin_sha256": "00" * 32})
        result = connector.test_connection(GOOD, changed)
        assert result["success"] is False and result["error_code"] == "CERTIFICATE_CHANGED"
        assert "certificate has changed" in result["error"] and "re-pin" in result["error"]
        assert fingerprint in result["error"]  # so the person can see what it is now
    finally:
        server.shutdown()


def test_a_signing_in_binding_without_an_allowed_list_sends_nothing(stub):
    connector = GenericHttpConnector()
    config = json.loads(_config(stub))
    session = connector.refresh_session(GOOD, json.dumps(config), None)["session"]
    before = len(stub.requests)
    for missing in (None, []):
        loose = dict(config)
        loose.pop("allowed")
        if missing is not None:
            loose["allowed"] = missing
        for action in ("GET /api/data", "POST /api/write", "call_endpoint"):
            result = connector.execute(
                action, {"method": "DELETE", "path": "/api/data"} if action == "call_endpoint" else {},
                GOOD, json.dumps(loose), session=session,
            )
            assert result["error_code"] == "REQUEST_NOT_ALLOWED"
            assert "fixed list of allowed requests" in result["error"]
    assert len(stub.requests) == before and stub.writes == 0


def test_caller_headers_are_ignored_when_requests_are_fixed(stub):
    seen = []

    class _Echo(_Stub):
        def do_GET(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            super().do_GET()

    stub.RequestHandlerClass = _Echo
    connector = GenericHttpConnector()
    config = _config(stub, headers={"X-Fixed": "from-binding"})
    session = connector.refresh_session(GOOD, config, None)["session"]
    result = connector.execute(
        "GET /api/data",
        {"headers": {"X-HTTP-Method-Override": "DELETE", "Host": "elsewhere.example", "X-Fixed": "caller"}},
        GOOD, config, session=session,
    )
    assert result["success"]
    assert "x-http-method-override" not in seen[-1] and seen[-1]["x-fixed"] == "from-binding"
    assert not seen[-1]["host"].startswith("elsewhere")
