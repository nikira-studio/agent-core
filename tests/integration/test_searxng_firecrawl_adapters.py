"""Integration tests for the SearXNG and Firecrawl adapter manifests."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate

SEARXNG_PATH = Path("app/adapter_templates/searxng/adapter.json")
FIRECRAWL_PATH = Path("app/adapter_templates/firecrawl/adapter.json")

SEARXNG_CONFIG = '{"base_url": "https://search.example.com"}'
FIRECRAWL_CONFIG = '{"base_url": "https://firecrawl.example.com"}'


def make_engine(path: Path, connector_id: str) -> HttpEngine:
    manifest, err = load_and_validate(path)
    assert err is None, f"manifest failed validation: {err}"
    return HttpEngine({"id": connector_id, "backend_json": json.dumps(manifest.backend)})


def mock_response(engine: HttpEngine, payload, calls: list) -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    engine._send = MagicMock(
        side_effect=lambda req, config: calls.append(req)
        or MagicMock(status=200, read=MagicMock(return_value=body))
    )
    engine._raise_on_errors = MagicMock()


class TestSearxngAdapterManifest:
    def test_manifest_validates_as_read_only_no_auth_adapter(self):
        manifest, err = load_and_validate(SEARXNG_PATH)
        assert err is None

        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "none"
        assert row["backend_type"] == "http"
        assert json.loads(row["required_credential_fields_json"]) == []
        assert {a["side_effect"] for a in manifest.actions} == {"read"}
        assert {a["name"] for a in manifest.actions} == {
            "healthcheck",
            "search",
            "get_config",
        }

    def test_healthcheck_is_the_test_action_and_needs_no_credential(self):
        manifest, _ = load_and_validate(SEARXNG_PATH)
        assert manifest.backend["test_action"] == "healthcheck"

        engine = make_engine(SEARXNG_PATH, "searxng")
        calls: list = []
        mock_response(engine, b"OK", calls)

        result = engine.execute("healthcheck", {}, None, SEARXNG_CONFIG, session=None)

        assert result["success"] is True
        assert calls[0]["url"] == "https://search.example.com/healthz"
        assert "Authorization" not in calls[0]["headers"]

    def test_search_forces_json_format_and_omits_unset_filters(self):
        engine = make_engine(SEARXNG_PATH, "searxng")
        calls: list = []
        mock_response(engine, {"query": "cats", "results": []}, calls)

        result = engine.execute(
            "search", {"q": "cats"}, None, SEARXNG_CONFIG, session=None
        )

        assert result["success"] is True
        url = calls[0]["url"]
        assert url.startswith("https://search.example.com/search?")
        assert "q=cats" in url
        assert "format=json" in url
        for omitted in ("categories=", "engines=", "language=", "safesearch=", "time_range=", "pageno="):
            assert omitted not in url

    def test_search_maps_page_to_pageno_and_passes_optional_filters(self):
        engine = make_engine(SEARXNG_PATH, "searxng")
        calls: list = []
        mock_response(engine, {"query": "cats", "results": []}, calls)

        engine.execute(
            "search",
            {
                "q": "cats and dogs",
                "categories": "general,news",
                "engines": "duckduckgo",
                "language": "en-US",
                "safesearch": 1,
                "time_range": "month",
                "page": 2,
            },
            None,
            SEARXNG_CONFIG,
            session=None,
        )

        url = calls[0]["url"]
        assert "q=cats+and+dogs" in url
        assert "categories=general%2Cnews" in url
        assert "engines=duckduckgo" in url
        assert "language=en-US" in url
        assert "safesearch=1" in url
        assert "time_range=month" in url
        assert "pageno=2" in url
        assert "page=2" not in url.replace("pageno=2", "")


class TestFirecrawlAdapterManifest:
    def test_manifest_validates_with_optional_bearer_credential(self):
        manifest, err = load_and_validate(FIRECRAWL_PATH)
        assert err is None

        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "bearer"
        assert row["backend_type"] == "http"
        # api_key is optional, so no credential is required to use a binding.
        assert json.loads(row["required_credential_fields_json"]) == []
        assert {a["name"] for a in manifest.actions} == {
            "healthcheck",
            "scrape",
            "crawl",
            "get_crawl_status",
            "cancel_crawl",
            "map",
            "search",
            "batch_scrape",
            "get_batch_scrape_status",
            "extract",
            "get_extract_status",
        }
        assert manifest.backend["test_action"] == "healthcheck"

    def test_scrape_posts_v2_path_without_auth_when_no_credential(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True, "data": {"markdown": "# hi"}}, calls)

        result = engine.execute(
            "scrape",
            {"url": "https://example.com", "formats": ["markdown"]},
            None,
            FIRECRAWL_CONFIG,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["method"] == "POST"
        assert req["url"] == "https://firecrawl.example.com/v2/scrape"
        assert req["body"] == {"url": "https://example.com", "formats": ["markdown"]}
        assert "Authorization" not in req["headers"]
        assert req["headers"]["User-Agent"] == "Agent-Core Firecrawl Connector/1.0"

    def test_scrape_sends_bearer_token_when_credential_present(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True}, calls)

        engine.execute(
            "scrape",
            {"url": "https://example.com"},
            Credential(raw="fc-test-key", fields={}, reference_name="test-cref"),
            FIRECRAWL_CONFIG,
            session=None,
        )

        assert calls[0]["headers"]["Authorization"] == "Bearer fc-test-key"

    def test_auth_mode_none_suppresses_bearer_header(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True}, calls)

        engine.execute(
            "scrape",
            {"url": "https://example.com"},
            Credential(raw="fc-test-key", fields={}),
            '{"base_url": "https://firecrawl.example.com", "auth_mode": "none"}',
            session=None,
        )

        assert "Authorization" not in calls[0]["headers"]

    def test_scrape_body_omits_unset_options_and_keeps_native_types(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True}, calls)

        engine.execute(
            "scrape",
            {
                "url": "https://example.com",
                "formats": ["markdown", "links"],
                "only_main_content": False,
                "wait_for": 1500,
                "mobile": True,
            },
            None,
            FIRECRAWL_CONFIG,
            session=None,
        )

        assert calls[0]["body"] == {
            "url": "https://example.com",
            "formats": ["markdown", "links"],
            "onlyMainContent": False,
            "waitFor": 1500,
            "mobile": True,
        }

    def test_api_prefix_config_overrides_the_v2_default(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True}, calls)

        engine.execute(
            "scrape",
            {"url": "https://example.com"},
            None,
            '{"base_url": "https://firecrawl.example.com", "api_prefix": "/v1"}',
            session=None,
        )

        assert calls[0]["url"] == "https://firecrawl.example.com/v1/scrape"

    def test_crawl_passes_scrape_options_object_through_verbatim(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True, "id": "job-1"}, calls)

        engine.execute(
            "crawl",
            {
                "url": "https://example.com",
                "limit": 5,
                "scrape_options": {"formats": ["markdown"], "onlyMainContent": True},
            },
            None,
            FIRECRAWL_CONFIG,
            session=None,
        )

        req = calls[0]
        assert req["url"] == "https://firecrawl.example.com/v2/crawl"
        assert req["body"] == {
            "url": "https://example.com",
            "limit": 5,
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        }

    def test_job_status_and_cancel_interpolate_the_job_id(self):
        engine = make_engine(FIRECRAWL_PATH, "firecrawl")
        calls: list = []
        mock_response(engine, {"success": True, "status": "completed"}, calls)

        engine.execute(
            "get_crawl_status", {"id": "job-1", "skip": 10}, None, FIRECRAWL_CONFIG
        )
        engine.execute("cancel_crawl", {"id": "job-1"}, None, FIRECRAWL_CONFIG)
        engine.execute(
            "get_batch_scrape_status", {"id": "job-2"}, None, FIRECRAWL_CONFIG
        )
        engine.execute("get_extract_status", {"id": "job-3"}, None, FIRECRAWL_CONFIG)

        assert calls[0]["url"] == "https://firecrawl.example.com/v2/crawl/job-1?skip=10"
        assert calls[0]["method"] == "GET"
        assert calls[1]["url"] == "https://firecrawl.example.com/v2/crawl/job-1"
        assert calls[1]["method"] == "DELETE"
        assert (
            calls[2]["url"]
            == "https://firecrawl.example.com/v2/batch/scrape/job-2"
        )
        assert calls[3]["url"] == "https://firecrawl.example.com/v2/extract/job-3"
