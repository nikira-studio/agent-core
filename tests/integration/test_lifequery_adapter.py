"""Integration tests for the LifeQuery adapter manifest.

Loads the real shipped template (app/adapter_templates/lifequery/adapter.json)
so the test guards the actual file an instance installs. The HTTP transport is
mocked; assertions cover request shape (paths, query params, body type
preservation, Bearer auth) and response handling.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate

ADAPTER_PATH = Path("app/adapter_templates/lifequery/adapter.json")
CONFIG_JSON = '{"base_url": "https://lq.example.com"}'


def make_engine() -> HttpEngine:
    manifest, err = load_and_validate(ADAPTER_PATH)
    assert err is None, f"manifest failed validation: {err}"
    return HttpEngine({"id": "lifequery", "backend_json": json.dumps(manifest.backend)})


def mock_response(engine: HttpEngine, payload: dict, calls: list) -> None:
    engine._send = MagicMock(
        side_effect=lambda req, config: calls.append(req)
        or MagicMock(status=200, read=MagicMock(return_value=json.dumps(payload).encode()))
    )
    engine._raise_on_errors = MagicMock()


def cred() -> Credential:
    return Credential(raw="LQ_KEY_123", fields={}, reference_name="test-cref")


class TestLifeQueryAdapterManifest:
    def test_manifest_validates_and_is_bearer_read_only(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "bearer"
        assert json.loads(row["required_credential_fields_json"]) == ["api_token"]
        # LifeQuery is a memory engine — every action must be read-only.
        assert {a["side_effect"] for a in manifest.actions} == {"read"}
        assert {a["name"] for a in manifest.actions} == {
            "list_chats",
            "list_people",
            "query_messages",
            "query_chunks",
            "summarize_range",
        }

    def test_list_chats_get_query_params_and_bearer(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"chats": [{"chat_id": "1"}], "count": 1}, calls)

        result = engine.execute(
            "list_chats",
            {"included_only": True, "limit": 25},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        assert result["body"]["count"] == 1
        req = calls[0]
        assert req["method"] == "GET"
        assert req["url"].startswith("https://lq.example.com/api/agent/chats?")
        assert "included_only=True" in req["url"]
        assert "limit=25" in req["url"]
        # chat_type was not supplied — it must not appear in the query string.
        assert "chat_type" not in req["url"]
        assert req["headers"]["Authorization"] == "Bearer LQ_KEY_123"

    def test_query_messages_preserves_types_and_drops_unset(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine, {"messages": [], "count": 0, "next_cursor": None}, calls
        )

        params = {
            "start": "2026-06-01T00:00:00Z",
            "end": "2026-06-08T00:00:00Z",
            "chat_ids": ["12345", "678"],
            "chat_types": ["group"],
            "included_only": False,
            "limit": 200,
            "order": "desc",
        }
        result = engine.execute("query_messages", params, cred(), CONFIG_JSON, session=None)

        assert result["success"] is True
        assert result["body"]["count"] == 0
        req = calls[0]
        assert req["method"] == "POST"
        assert req["url"] == "https://lq.example.com/api/agent/messages/query"
        body = json.loads(json.dumps(req["body"]))
        # Required strings pass through verbatim.
        assert body["start"] == "2026-06-01T00:00:00Z"
        assert body["end"] == "2026-06-08T00:00:00Z"
        # Arrays / bool / int keep their JSON types (not stringified).
        assert body["chat_ids"] == ["12345", "678"]
        assert body["chat_types"] == ["group"]
        assert body["included_only"] is False
        assert body["limit"] == 200
        assert body["order"] == "desc"
        # Unset optionals are omitted entirely.
        for absent in ("chat_names", "sender_ids", "sender_names", "sources",
                       "text_query", "cursor"):
            assert absent not in body
        assert req["headers"]["Authorization"] == "Bearer LQ_KEY_123"

    def test_summarize_range_passes_prompt_and_include_messages(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine,
            {"summary": "ok", "message_count": 3, "next_cursor": None, "messages": None},
            calls,
        )

        params = {
            "start": "2026-06-01T00:00:00Z",
            "end": "2026-06-08T00:00:00Z",
            "prompt": "focus on decisions",
            "include_messages": True,
        }
        result = engine.execute("summarize_range", params, cred(), CONFIG_JSON, session=None)

        assert result["success"] is True
        assert result["body"]["summary"] == "ok"
        body = json.loads(json.dumps(calls[0]["body"]))
        assert calls[0]["url"] == "https://lq.example.com/api/agent/summary"
        assert body["prompt"] == "focus on decisions"
        assert body["include_messages"] is True
        assert "text_query" not in body
