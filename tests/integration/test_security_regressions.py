"""Executable regressions for vulnerabilities reproduced during security review."""

import asyncio
import socket
import stat
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_concurrent_bootstrap_creates_exactly_one_admin(clean_db, monkeypatch):
    from app.services import auth_service

    barrier = threading.Barrier(2)

    def synchronized_hash(_password: str) -> str:
        barrier.wait(timeout=5)
        return "$2b$12$eImiTXuWVxfM37uY4JANjuiGlLJCXMEuFSijlTEqvWwGY0e6Zg3E2"

    monkeypatch.setattr(auth_service, "hash_password", synchronized_hash)

    def register(number: int):
        try:
            return auth_service.create_initial_admin(
                f"admin{number}",
                f"admin{number}@example.test",
                "password",
                f"Admin {number}",
            )
        except auth_service.RegistrationDisabledError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, (1, 2)))

    assert sum(result is not None for result in results) == 1
    assert auth_service.count_users() == 1


def test_chunked_body_over_limit_is_rejected_before_json_parsing(test_client):
    oversized_json = b'{"email":"' + (b"a" * (1024 * 1024)) + b'"}'

    response = test_client.post(
        "/api/auth/login",
        headers={"Content-Type": "application/json"},
        content=iter((oversized_json[:500_000], oversized_json[500_000:])),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_request_size_middleware_preserves_streaming_responses():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import StreamingResponse

    from app.security.request_size import RequestBodyLimitMiddleware

    stream_app = FastAPI()
    stream_app.add_middleware(RequestBodyLimitMiddleware, max_bytes=1024)

    @stream_app.get("/stream")
    def stream():
        return StreamingResponse(iter((b"first\n", b"second\n")))

    with TestClient(stream_app) as client:
        response = client.get("/stream")

    assert response.status_code == 200
    assert response.content == b"first\nsecond\n"


def test_request_size_middleware_preserves_bodyless_get_receive_channel():
    from app.security.request_size import RequestBodyLimitMiddleware

    seen = []

    async def downstream(_scope, receive, send):
        seen.append(receive)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        raise AssertionError("a bodyless GET should not be consumed")

    async def send(_message):
        return None

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=1024)
    for headers in ([], [(b"content-length", b"0")]):
        asyncio.run(
            middleware(
                {"type": "http", "method": "GET", "headers": headers},
                receive,
                send,
            )
        )

    assert seen == [receive, receive]


def test_exhausted_login_limit_skips_password_hash(test_client, clean_db, monkeypatch):
    from app.security.rate_limiter import RL
    from app.services.auth_service import create_user
    import app.routes.auth as auth_route

    create_user("user", "user@example.test", "password123", "User")
    for _ in range(10):
        RL.check("user", "testclient", "login_failed")

    calls = 0

    def counted_verify(_password: str, _password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(auth_route, "verify_password", counted_verify)
    response = test_client.post(
        "/api/auth/login",
        json={"email": "user@example.test", "password": "wrong"},
    )

    assert response.status_code == 429
    assert calls == 0


def test_safe_urlopen_connects_only_to_validated_address(monkeypatch):
    from app.config import settings
    from app.security import safe_http
    from app.security import url_validation

    resolutions = 0
    attempted = []

    def public_resolution(host, port, **_kwargs):
        nonlocal resolutions
        assert host == "rebind.example.test"
        resolutions += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def refuse_connection(family, sockaddr, timeout, source_address=None):
        attempted.append((family, sockaddr))
        raise OSError("intentional test refusal")

    monkeypatch.setattr(settings, "BLOCK_INTERNAL_HOSTS", True)
    monkeypatch.setattr(url_validation.socket, "getaddrinfo", public_resolution)
    monkeypatch.setattr(safe_http, "_connect_address", refuse_connection)

    with pytest.raises(urllib.error.URLError):
        safe_http.safe_urlopen("http://rebind.example.test/resource", timeout=1)

    assert resolutions == 1
    assert attempted == [(socket.AF_INET, ("93.184.216.34", 80))]


def test_stored_webhook_is_revalidated_at_delivery(clean_db, monkeypatch):
    from app.config import settings
    from app.services import webhook_service

    webhook = webhook_service.create_webhook(
        "local",
        "http://127.0.0.1:9/hook",
        "secret",
        ["activity_created"],
        "admin",
    )
    monkeypatch.setattr(settings, "BLOCK_INTERNAL_HOSTS", True)

    result = webhook_service.test_delivery(webhook["id"])

    assert result["ok"] is False
    assert "Blocked private network host" in result["error"]


def test_mcp_endpoint_is_revalidated_at_execution(monkeypatch):
    from app.config import settings
    from app.services import mcp_provider_service

    monkeypatch.setattr(settings, "BLOCK_INTERNAL_HOSTS", True)
    result = mcp_provider_service.execute_mcp_tool("http://127.0.0.1:9/mcp", "example")

    assert result.success is False
    assert result.error_code == "INVALID_URL"


def test_data_directory_and_database_are_owner_only(clean_db):
    directory_mode = stat.S_IMODE(clean_db.parent.stat().st_mode)
    database_mode = stat.S_IMODE(clean_db.stat().st_mode)

    assert directory_mode == 0o700
    assert database_mode == 0o600
