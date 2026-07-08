"""Integration tests for the HealthQuery adapter manifest."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate

ADAPTER_PATH = Path("app/adapter_templates/healthquery/adapter.json")
CONFIG_JSON = '{"base_url": "https://hq.example.com"}'


def make_engine() -> HttpEngine:
    manifest, err = load_and_validate(ADAPTER_PATH)
    assert err is None, f"manifest failed validation: {err}"
    return HttpEngine({"id": "healthquery", "backend_json": json.dumps(manifest.backend)})


def mock_response(engine: HttpEngine, payload: dict, calls: list) -> None:
    engine._send = MagicMock(
        side_effect=lambda req, config: calls.append(req)
        or MagicMock(status=200, read=MagicMock(return_value=json.dumps(payload).encode()))
    )
    engine._raise_on_errors = MagicMock()


def cred() -> Credential:
    return Credential(raw="HQ_READ_TOKEN", fields={}, reference_name="test-cref")


class TestHealthQueryAdapterManifest:
    def test_manifest_validates_as_required_bearer_read_only_adapter(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None

        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "bearer"
        assert row["backend_type"] == "http"
        assert json.loads(row["required_credential_fields_json"]) == ["read_token"]
        assert {a["side_effect"] for a in manifest.actions} == {"read"}
        assert {a["name"] for a in manifest.actions} == {
            "get_health_check",
            "get_health_status",
            "get_health_overview",
            "get_health_summary",
            "get_health_activity",
            "get_health_sleep",
            "get_health_vitals",
            "get_health_body",
            "get_health_timeline",
            "get_health_batches",
            "get_health_config",
            "execute_health_query",
            "generate_doctor_visit_report",
            "ask_health_question",
        }

    def test_get_health_status_uses_configured_base_url_and_bearer(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine,
            {"status": "ok", "counts": {"ingest_batches": 2}},
            calls,
        )

        result = engine.execute("get_health_status", {}, cred(), CONFIG_JSON, session=None)

        assert result["success"] is True
        assert result["body"]["status"] == "ok"
        req = calls[0]
        assert req["method"] == "GET"
        assert req["url"] == "https://hq.example.com/api/health/status"
        assert req["headers"]["Authorization"] == "Bearer HQ_READ_TOKEN"

    def test_timeline_and_batches_render_optional_query_params(self):
        engine = make_engine()
        timeline_calls: list = []
        mock_response(engine, {"events": [], "days": 7}, timeline_calls)

        result = engine.execute(
            "get_health_timeline",
            {"days": 7},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        assert timeline_calls[0]["url"] == "https://hq.example.com/api/health/timeline?days=7"

        batch_calls: list = []
        mock_response(engine, {"batches": []}, batch_calls)
        result = engine.execute(
            "get_health_batches",
            {},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        assert batch_calls[0]["url"] == "https://hq.example.com/api/health/batches"

    def test_execute_health_query_posts_sql_body(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine,
            {
                "sql": "SELECT COUNT(*) AS count FROM workouts",
                "row_count": 1,
                "returned_row_count": 1,
                "byte_count": 13,
                "truncated": False,
                "rows": [{"count": 3}],
            },
            calls,
        )

        result = engine.execute(
            "execute_health_query",
            {"sql": "SELECT COUNT(*) AS count FROM workouts"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["method"] == "POST"
        assert req["url"] == "https://hq.example.com/api/health/query"
        assert req["body"] == {"sql": "SELECT COUNT(*) AS count FROM workouts"}

    def test_doctor_visit_report_posts_dates_and_stream_false(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine,
            {"status": "success", "mode": "deterministic", "report": {}},
            calls,
        )

        result = engine.execute(
            "generate_doctor_visit_report",
            {"start_date": "2026-06-01", "end_date": "2026-06-20"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"] == "https://hq.example.com/api/reports/doctor-visit"
        assert req["body"] == {
            "stream": False,
            "start_date": "2026-06-01",
            "end_date": "2026-06-20",
        }

    def test_ask_health_question_omits_unset_dates(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"answer": "summary", "model": None}, calls)

        result = engine.execute(
            "ask_health_question",
            {"question": "How did my sleep change this week?"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        req = calls[0]
        assert req["url"] == "https://hq.example.com/api/reports/ask"
        assert req["body"] == {"question": "How did my sleep change this week?"}
