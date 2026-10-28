"""Regressions for the broader review. All network sends and keys are isolated."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
import pytest

from app.connectors.base import Credential
from app.connectors.cli_engine import CliEngine
from app.connectors.http_engine import HttpEngine
from app.database import get_db
from app.security.context import build_user_context
from app.security.encryption import decrypt_value
from app.services import auth_service, connector_service, workspace_service


@pytest.mark.asyncio
async def test_sse_does_not_disclose_private_activity_to_unrelated_user(clean_db):
    from app.routes.events import stream_events
    from app.services import activity_service

    auth_service.create_user(
        "review-stranger",
        "stranger@review.invalid",
        "testpassword123",
        "Stranger",
        "user",
    )
    session = auth_service.create_session("review-stranger")
    session["user_id"] = "review-stranger"
    assert "workspace:private-review" not in build_user_context(session).read_scopes
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await stream_events(request, session)
    try:
        activity_service.notify(
            "activity_created",
            {
                "id": "private-review-activity",
                "memory_scope": "workspace:private-review",
                "task_description": "Private unreleased roadmap",
                "user_id": "other-owner",
            },
        )
        try:
            chunk = await asyncio.wait_for(
                response.body_iterator.__anext__(), timeout=0.1
            )
        except asyncio.TimeoutError:
            return
        assert "private-review-activity" not in chunk
    finally:
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_read_only_connection_test_does_not_execute_disabled_delete(
    clean_db, monkeypatch
):
    from app.routes.connectors import test_binding as route_test_binding
    from app.security.context import RequestContext
    from app.security.effective_authority import permanent_authority

    connector_service.create_connector_type(
        "review-delete",
        "Delete-only adapter",
        auth_type="none",
        backend_type="http",
        supported_actions=[{"name": "delete_all", "side_effect": "destructive"}],
        disabled_actions=["delete_all"],
        backend_json=json.dumps(
            {
                "base_url": "https://review.invalid",
                "auth": {"type": "none"},
                "requests": {"delete_all": {"method": "DELETE", "path": "/all"}},
            }
        ),
    )
    binding = connector_service.create_binding(
        "review-delete", "review", "workspace:review"
    )
    sends = []

    def fake_send(self, request, config):
        sends.append(request["method"])
        return SimpleNamespace(status=200, headers={}, read=lambda: b"{}")

    monkeypatch.setattr(HttpEngine, "_send", fake_send)
    authority = permanent_authority(
        RequestContext(
            actor_type="user",
            actor_id="review-reader",
            read_scopes=["workspace:review"],
            capabilities=frozenset({"connectors_read"}),
            active_workspace_ids=frozenset({"review"}),
        )
    )
    response = await route_test_binding(binding["id"], authority)
    assert response.status_code < 500
    assert sends == [], f"Connection test sent {sends} with read-only authority"


@pytest.mark.parametrize("text", ["review fixture", ""])
def test_cli_stdin_executes_with_real_subprocess(text):
    engine = CliEngine(
        {
            "backend_json": json.dumps(
                {
                    "bin": "/usr/bin/cat",
                    "commands": {
                        "echo": {
                            "args": [],
                            "stdin": "{{ params.text }}",
                            "parse": {"type": "text"},
                        }
                    },
                }
            )
        }
    )
    result = engine.execute("echo", {"text": text}, Credential(None), "{}")
    assert result["success"] is True
    assert result["output"] == text


def test_key_restore_rejects_key_that_cannot_decrypt_noncredential_secrets(clean_db):
    from app.services import credential_rotation_service, webhook_service

    hook = webhook_service.create_webhook(
        "review",
        "https://review.invalid",
        "fixture-secret",
        ["activity_created"],
        "review",
    )
    with get_db() as conn:
        ciphertext = conn.execute(
            "SELECT secret_encrypted FROM webhook_registrations WHERE id=?",
            (hook["id"],),
        ).fetchone()[0]
    ok, _ = credential_rotation_service.restore_key("review", Fernet.generate_key())
    try:
        preserved = decrypt_value(ciphertext) == "fixture-secret"
    except InvalidToken:
        preserved = False
    assert (ok, preserved) == (False, True)


def test_user_authority_batches_workspace_permissions(clean_db):
    auth_service.create_user(
        "review-owner", "owner@review.invalid", "testpassword123", "Owner", "user"
    )
    for index in range(30):
        workspace_service.create_workspace(
            f"review-{index}", f"Review {index}", "review-owner"
        )
    with patch.object(
        workspace_service, "get_db", wraps=workspace_service.get_db
    ) as reads:
        context = build_user_context({"user_id": "review-owner", "role": "user"})
    assert len(context.read_scopes) == 31
    assert (
        reads.call_count <= 2
    ), f"{reads.call_count} database connections for 30 workspaces"


@pytest.mark.parametrize(
    "body",
    [[], None, {"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": [1]}],
)
def test_mcp_rejects_malformed_envelope_without_server_error(
    test_client, agent_token, body
):
    client = TestClient(test_client.app, raise_server_exceptions=False)
    try:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {agent_token}",
                "Content-Type": "application/json",
            },
            content=json.dumps(body),
        )
    finally:
        client.close()
    assert response.status_code < 500


@pytest.mark.parametrize(
    ("event_type", "data", "visible"),
    [
        (
            "activity_updated",
            {"memory_scope": "workspace:review", "user_id": "review-reader"},
            True,
        ),
        ("activity_updated", {"memory_scope": "workspace:other"}, False),
        (
            "activity_updated",
            {"memory_scope": "user:review-reader", "user_id": "review-reader"},
            True,
        ),
        ("activity_updated", {"user_id": "review-reader"}, True),
        ("activity_updated", {"user_id": "other"}, False),
        (
            "activity_updated",
            {"user_id": "other", "memory_scope": "workspace:review"},
            False,
        ),
        (
            "activity_updated",
            {"user_id": "review-reader", "memory_scope": "agent:private"},
            True,
        ),
        ("connector_executed", {"scope": "workspace:review"}, True),
        ("connector_executed", {"scope": "workspace:other"}, False),
        ("connector_executed", {}, False),
        (
            "delegation_request_created",
            {"target_user_id": "review-reader", "requester_actor_id": "review-reader"},
            False,
        ),
        ("unknown_event", {"scope": "workspace:review"}, False),
    ],
)
def test_event_visibility_uses_resource_permissions(
    clean_db, event_type, data, visible
):
    from app.routes.events import _event_visible

    auth_service.create_user(
        "review-reader", "reader@review.invalid", "testpassword123", "Reader", "user"
    )
    workspace_service.create_workspace("review", "Review", "review-reader")
    assert (
        _event_visible(
            {"type": event_type, "data": data},
            {"user_id": "review-reader", "role": "user"},
        )
        is visible
    )


@pytest.mark.asyncio
async def test_open_event_stream_refreshes_permissions_and_session(clean_db):
    from app.routes.events import stream_events
    from app.services import activity_service
    from app.services.event_stream_service import event_hub

    auth_service.create_user(
        "review-reader", "reader@review.invalid", "testpassword123", "Reader", "user"
    )
    workspace_service.create_workspace("review", "Review", "review-reader")
    session = auth_service.create_session("review-reader")
    response = await stream_events(
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), session
    )
    count = event_hub.subscriber_count
    try:
        event_hub.publish(
            "connector_executed", {"binding_id": "first", "scope": "workspace:review"}
        )
        assert "first" in await response.body_iterator.__anext__()
        workspace_service.update_workspace("review", is_active=False)
        event_hub.publish(
            "connector_executed", {"binding_id": "revoked", "scope": "workspace:review"}
        )
        activity_service.notify(
            "activity_created",
            {
                "id": "still-visible",
                "memory_scope": "user:review-reader",
                "user_id": "review-reader",
            },
        )
        chunk = await asyncio.wait_for(response.body_iterator.__anext__(), 1)
        assert "still-visible" in chunk and "revoked" not in chunk
        auth_service.delete_session(session["db_session_id"])
        activity_service.notify(
            "activity_created",
            {
                "id": "after-logout",
                "memory_scope": "user:review-reader",
                "user_id": "review-reader",
            },
        )
        with pytest.raises(StopAsyncIteration):
            await response.body_iterator.__anext__()
        assert event_hub.subscriber_count == count - 1
    finally:
        await response.body_iterator.aclose()


@pytest.mark.parametrize(
    "side_effect,method,disabled,required",
    [
        ("destructive", "DELETE", False, False),
        ("write", "POST", False, False),
        ("read", "POST", True, False),
        ("read", "POST", False, True),
        (None, "POST", False, False),
    ],
)
def test_http_test_action_controls_precede_transport(
    side_effect, method, disabled, required, monkeypatch
):
    action = {
        "name": "ping",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"] if required else [],
        },
    }
    if side_effect is not None:
        action["side_effect"] = side_effect
    engine = HttpEngine(
        {
            "supported_actions": [action],
            "disabled_actions": ["ping"] if disabled else [],
            "backend_json": json.dumps(
                {
                    "test_action": "ping",
                    "requests": {"ping": {"method": method, "path": "/probe"}},
                }
            ),
        }
    )
    send = AsyncMock()
    monkeypatch.setattr(engine, "_send", send)
    assert not engine.test_connection(Credential(None), "{}")["success"]
    send.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_generic_http_test_method_cannot_mutate(method, monkeypatch):
    from app.connectors.generic_http import GenericHttpConnector

    engine = GenericHttpConnector()
    call = AsyncMock()
    monkeypatch.setattr(engine, "_call", call)
    result = engine.test_connection(
        None, json.dumps({"base_url": "https://review.invalid", "test_method": method})
    )
    assert not result["success"]
    call.assert_not_called()


@pytest.mark.parametrize("store", ["otp", "pending_otp", "session", "vector", "spill"])
def test_key_restore_checks_every_noncredential_encrypted_store(clean_db, store):
    from app.security import encryption
    from app.services import credential_rotation_service, vector_settings_service

    auth_service.create_user(
        "review-key", "key@review.invalid", "testpassword123", "Key owner", "user"
    )
    ciphertext = encryption.encrypt_value("fixture-secret")
    candidate = Fernet.generate_key()
    if store == "pending_otp":
        candidate = encryption.get_primary_key()
        encryption.rotate_key()
        pending_ciphertext = encryption.encrypt_value("pending-secret")
    with get_db() as conn:
        if store == "otp":
            conn.execute(
                "INSERT INTO otp_secrets (user_id, secret_encrypted) VALUES (?, ?)",
                ("review-key", ciphertext),
            )
        elif store == "pending_otp":
            conn.execute(
                "INSERT INTO otp_secrets (user_id, secret_encrypted, pending_secret_encrypted) "
                "VALUES (?, ?, ?)",
                ("review-key", ciphertext, pending_ciphertext),
            )
        elif store == "vector":
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                ("vector_api_key", ciphertext),
            )
        elif store == "spill":
            conn.execute(
                "INSERT INTO tool_result_spill (id, content, total_chars) VALUES (?, ?, ?)",
                ("review-spill", ciphertext, 14),
            )
        else:
            connector_service.create_connector_type(
                "review-key", "Key test", auth_type="none"
            )
            binding = connector_service.create_binding(
                "review-key", "Key test", "user:review-key"
            )
            conn.execute(
                "INSERT INTO connector_session_cache (binding_id, session_data_encrypted) "
                "VALUES (?, ?)",
                (binding["id"], ciphertext),
            )
    before = encryption.get_keyring()
    ok, message = credential_rotation_service.restore_key("review-key", candidate)
    assert not ok and "cannot decrypt" in message
    assert encryption.get_keyring() == before
    assert decrypt_value(ciphertext) == "fixture-secret"
    if store == "pending_otp":
        assert decrypt_value(pending_ciphertext) == "pending-secret"
        return
    ok, message = credential_rotation_service.restore_key("review-key", before[0])
    assert ok, message
    encryption.reset_key_cache()
    assert decrypt_value(ciphertext) == "fixture-secret"
    if store == "vector":
        assert (
            vector_settings_service.get_vector_setting("vector_api_key")
            == "fixture-secret"
        )


@pytest.mark.parametrize(
    "body,code",
    [
        ({"jsonrpc": "1.0", "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "method": []}, -32600),
        ({"jsonrpc": "2.0", "method": "ping", "id": []}, -32600),
        ({"jsonrpc": "2.0", "method": "tools/call", "params": None}, -32602),
        ({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": []}}, -32602),
        (
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_get", "arguments": []},
            },
            -32602,
        ),
    ],
)
def test_mcp_returns_protocol_errors_for_invalid_shapes(
    test_client, agent_token, body, code
):
    response = test_client.post(
        "/mcp", headers={"Authorization": f"Bearer {agent_token}"}, json=body
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "restriction", ["disabled", "required_query", "required_body", "write"]
)
def test_openapi_connection_probe_skips_unsafe_operations(restriction, monkeypatch):
    from unittest.mock import MagicMock
    from app.connectors.openapi_executor import OpenApiExecutor

    operation = {"operation_id": "probe", "method": "GET", "path": "/probe"}
    metadata = {"supported_actions": [{"name": "probe", "side_effect": "read"}]}
    if restriction == "disabled":
        metadata["disabled_actions"] = ["probe"]
    elif restriction == "required_query":
        operation["parameters"] = [{"in": "query", "name": "id", "required": True}]
    elif restriction == "required_body":
        operation["request_body"] = {"required": True}
    else:
        metadata["supported_actions"][0]["side_effect"] = "write"
    executor = OpenApiExecutor(metadata)
    requests = []
    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr(
        "app.connectors.openapi_executor.validate_public_url", lambda _: None
    )

    def send(request, **kwargs):
        requests.append((request.method, request.full_url))
        return response

    monkeypatch.setattr("app.connectors.openapi_executor.safe_urlopen", send)
    result = executor.test_connection(
        None,
        json.dumps(
            {
                "_operations_json": {
                    "servers": [{"url": "https://review.invalid"}],
                    "operations": [operation],
                }
            }
        ),
    )
    assert result["success"]
    assert requests == [("HEAD", "https://review.invalid")]
