"""Integration tests for the WorldMonitor adapter manifest."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate

ADAPTER_PATH = Path("app/adapter_templates/worldmonitor/adapter.json")
CONFIG_JSON = '{"base_url": "https://worldmonitor.example.com"}'


def make_engine() -> HttpEngine:
    manifest, err = load_and_validate(ADAPTER_PATH)
    assert err is None, f"manifest failed validation: {err}"
    return HttpEngine({"id": "worldmonitor", "backend_json": json.dumps(manifest.backend)})


def mock_response(engine: HttpEngine, payload: dict, calls: list) -> None:
    engine._send = MagicMock(
        side_effect=lambda req, config: calls.append(req)
        or MagicMock(status=200, read=MagicMock(return_value=json.dumps(payload).encode()))
    )
    engine._raise_on_errors = MagicMock()


def cred() -> Credential:
    return Credential(raw="wm_test_key", fields={}, reference_name="test-cref")


class TestWorldMonitorAdapterManifest:
    def test_manifest_validates_as_read_only_api_key_adapter(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None

        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "api_key"
        assert row["backend_type"] == "http"
        assert json.loads(row["required_credential_fields_json"]) == []
        assert {a["side_effect"] for a in manifest.actions} == {"read"}
        assert {a["name"] for a in manifest.actions} == {
            "get_version",
            "get_platform_health",
            "list_acled_events",
            "list_cyber_threats",
            "get_country_risk",
            "get_country_intel_brief",
            "list_market_quotes",
            "get_fear_greed_index",
            "get_chokepoint_status",
            "list_natural_events",
            "list_feed_digest",
        }

    def test_get_version_uses_configured_base_url_and_worldmonitor_key_header(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"version": "1.2.3"}, calls)

        result = engine.execute("get_version", {}, cred(), CONFIG_JSON, session=None)

        assert result["success"] is True
        assert result["body"]["version"] == "1.2.3"
        req = calls[0]
        assert req["method"] == "GET"
        assert req["url"] == "https://worldmonitor.example.com/api/version"
        assert req["headers"]["X-WorldMonitor-Key"] == "wm_test_key"
        assert req["headers"]["User-Agent"] == "Agent-Core WorldMonitor Connector/1.0"
        assert "Authorization" not in req["headers"]

    def test_get_version_can_run_without_credential_when_auth_mode_none(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"version": "1.2.3"}, calls)

        result = engine.execute(
            "get_version",
            {},
            None,
            '{"base_url": "https://worldmonitor.example.com", "auth_mode": "none"}',
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"] == "https://worldmonitor.example.com/api/version"
        assert "X-WorldMonitor-Key" not in req["headers"]
        assert req["headers"]["User-Agent"] == "Agent-Core WorldMonitor Connector/1.0"

    def test_list_acled_events_renders_supplied_query_params_only(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"events": [], "nextCursor": "abc"}, calls)

        result = engine.execute(
            "list_acled_events",
            {
                "start": "1780000000000",
                "page_size": 25,
                "country": "US",
            },
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"].startswith(
            "https://worldmonitor.example.com/api/conflict/v1/list-acled-events?"
        )
        assert "start=1780000000000" in req["url"]
        assert "page_size=25" in req["url"]
        assert "country=US" in req["url"]
        assert "end=" not in req["url"]
        assert "cursor=" not in req["url"]
        assert req["headers"]["X-WorldMonitor-Key"] == "wm_test_key"

    def test_country_intel_brief_urlencodes_framework(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"brief": "ok"}, calls)

        result = engine.execute(
            "get_country_intel_brief",
            {"country_code": "JP", "framework": "focus on energy risk"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"].startswith(
            "https://worldmonitor.example.com/api/intelligence/v1/get-country-intel-brief?"
        )
        assert "country_code=JP" in req["url"]
        assert "framework=focus+on+energy+risk" in req["url"]

    def test_list_feed_digest_drops_unset_language(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"items": []}, calls)

        result = engine.execute(
            "list_feed_digest",
            {"variant": "finance"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"] == (
            "https://worldmonitor.example.com/api/news/v1/list-feed-digest?variant=finance"
        )
