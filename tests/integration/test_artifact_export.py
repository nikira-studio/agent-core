"""Integration test: connectors_run exports image data URLs as artifacts.

The end-to-end flow:

1. Patch `connector_service.execute_binding_action_with_logging` to return a
   controlled result that contains a `data:image/png;base64,...` payload.
2. Call `connectors_run` over the JSON-RPC MCP endpoint.
3. Confirm the response body now carries an artifact reference, the file
   exists on disk, and `result_fetch` is NOT used (because the data URL was
   replaced with a small path reference before the spill path ran).
4. Confirm a non-image oversized body is still offloaded via
   `tool_result_spill` — the artifact export only fires for image data URLs.
"""

import base64
import json
import os
from pathlib import Path


def _png_b64() -> str:
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + os.urandom(50_000)
    ).decode()


def _call_tool(client, token, name, arguments, rid=10):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _call_text(resp):
    return resp.json()["result"]["content"][0]["text"]


def _create_binding(test_client, admin_token, name="img-binding", scope="agent:testagent"):
    from app.services import connector_service, credential_service

    credential = credential_service.create_credential(
        scope=scope,
        name=f"{name}-token",
        value_plaintext="unused",
    )
    return connector_service.create_binding(
        connector_type_id="generic_http",
        name=name,
        scope=scope,
        credential_id=credential["id"],
    )


def test_connectors_run_exports_data_url_image(
    test_client, admin_token, agent_token, monkeypatch
):
    binding = _create_binding(test_client, admin_token)

    b64 = _png_b64()
    data_url = f"data:image/png;base64,{b64}"
    fake_result = {
        "success": True,
        "body": {"choices": [{"message": {"content": data_url}}]},
        "duration_ms": 12,
    }

    from app.services import connector_service

    # The route imports `connector_service` inside the function block, so
    # patching the module attributes is enough for the `connectors_run`
    # call site to pick them up.
    monkeypatch.setattr(
        connector_service,
        "execute_binding_action_with_logging",
        lambda binding_id, action, params: fake_result,
    )
    monkeypatch.setattr(
        connector_service, "get_binding", lambda bid: {
            "id": bid,
            "connector_type_id": "generic_http",
            "scope": "agent:testagent",
            "enabled": True,
            "name": "img-binding",
        }
    )
    monkeypatch.setattr(
        connector_service, "get_connector_type", lambda ctid: {
            "id": ctid,
            "auth_type": "none",
            "provider_type": "openapi",
            "supported_actions": ["call_endpoint"],
            "disabled_actions": [],
        }
    )
    monkeypatch.setattr(
        connector_service, "action_requires_write", lambda ct, action: False
    )

    r = _call_tool(
        test_client,
        agent_token,
        "connectors_run",
        {"binding_id": binding["id"], "action": "call_endpoint", "params": {}},
    )
    assert r.status_code == 200, r.json()
    text = _call_text(r)
    # The MCP wrapper drops the `{"ok": True, "data": ...}` envelope before
    # serialising the text content, so the text IS the inner `result` dict.
    inner = json.loads(text)
    assert "data" not in inner or "success" in inner, (
        "the connectors_run payload should be the result dict, not the "
        f"envelope; got: {text[:200]!r}"
    )
    assert inner["success"] is True
    exported = inner["body"]["choices"][0]["message"]["content"]
    assert exported["exported"] is True
    artifact = exported["artifact"]
    assert artifact["mime_type"] == "image/png"
    assert artifact["size_bytes"] == len(base64.b64decode(b64))
    artifact_path = Path(artifact["path"])
    assert artifact_path.is_file()
    # The bytes on disk are the decoded payload, not the base64 string.
    assert artifact_path.read_bytes() == base64.b64decode(b64)
    # The original data URL MUST NOT appear in the response anywhere.
    assert "data:image/png;base64," not in text
    # A count is exposed so the caller can branch on it without scanning.
    assert inner["artifacts_exported"] == 1
    # Total response stays small: the data URL was replaced with a tiny ref.
    assert len(text) < 5000


def test_connectors_run_non_image_oversized_body_still_spills(
    test_client, admin_token, agent_token, monkeypatch
):
    """The existing spill path must keep working for non-image data.

    Regression: the new artifact export only fires for `data:image/...`
    URLs. A 100 KB plain-text body (no data URL) should still be offloaded
    to `tool_result_spill` because that is the supported path for that
    shape today.
    """
    binding = _create_binding(test_client, admin_token, name="plain-binding")

    big_body = "plain text response " * 5000  # ~100 KB
    fake_result = {"success": True, "body": big_body, "duration_ms": 1}

    from app.services import connector_service

    monkeypatch.setattr(
        connector_service,
        "execute_binding_action_with_logging",
        lambda binding_id, action, params: fake_result,
    )
    monkeypatch.setattr(
        connector_service, "get_binding", lambda bid: {
            "id": bid,
            "connector_type_id": "generic_http",
            "scope": "agent:testagent",
            "enabled": True,
            "name": "plain-binding",
        }
    )
    monkeypatch.setattr(
        connector_service, "get_connector_type", lambda ctid: {
            "id": ctid,
            "auth_type": "none",
            "provider_type": "openapi",
            "supported_actions": ["call_endpoint"],
            "disabled_actions": [],
        }
    )
    monkeypatch.setattr(
        connector_service, "action_requires_write", lambda ct, action: False
    )

    r = _call_tool(
        test_client,
        agent_token,
        "connectors_run",
        {"binding_id": binding["id"], "action": "call_endpoint", "params": {}},
    )
    assert r.status_code == 200, r.json()
    text = _call_text(r)
    payload = json.loads(text)
    # Non-image oversized body lands in the spill payload, not in an
    # artifact. The data URL export must NOT have run.
    assert payload["offloaded"] is True
    assert payload["total_chars"] >= 8000
    assert "artifact" not in text
