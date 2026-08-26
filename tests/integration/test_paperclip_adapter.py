"""Integration tests for the Paperclip adapter manifest.

Regression coverage for a reported bug: get_issue/get_run single-record lookups
were rejected by JSON-schema validation. Root cause turned out to be a caller
sending an empty params object (not an Agent Core alias/schema/reload bug), but
get_run had no explicit runId->run_id alias declared (it worked anyway via the
generic camelCase->snake_case inference in connector_service._prepare_action_params,
see test_connector_action_params.py). These tests lock in both the declared
alias and the canonical param path at the manifest/wire level, the one layer
that wasn't previously covered for this adapter.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine
from app.connectors.manifest import load_and_validate

ADAPTER_PATH = Path("app/adapter_templates/paperclip/adapter.json")
CONFIG_JSON = '{"base_url": "https://paperclip.example.com"}'


def make_engine() -> HttpEngine:
    manifest, err = load_and_validate(ADAPTER_PATH)
    assert err is None, f"manifest failed validation: {err}"
    return HttpEngine({"id": "paperclip", "backend_json": json.dumps(manifest.backend)})


def mock_response(engine: HttpEngine, payload: dict, calls: list) -> None:
    engine._send = MagicMock(
        side_effect=lambda req, config: calls.append(req)
        or MagicMock(status=200, read=MagicMock(return_value=json.dumps(payload).encode()))
    )
    engine._raise_on_errors = MagicMock()


def cred() -> Credential:
    return Credential(raw="pcp_board_test", fields={}, reference_name="test-cref")


class TestPaperclipAdapterManifest:
    def test_manifest_validates(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        row = manifest.to_connector_type_row()
        assert row["auth_type"] == "bearer"
        assert row["backend_type"] == "http"

    def test_get_issue_declares_issue_id_alias(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        action = next(a for a in manifest.actions if a["name"] == "get_issue")
        assert action["param_aliases"] == {"issueId": "issue_id"}
        assert action["input_schema"]["required"] == ["issue_id"]
        assert "issueId" in action["input_schema"]["properties"]

    def test_get_run_declares_run_id_alias(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        action = next(a for a in manifest.actions if a["name"] == "get_run")
        assert action["param_aliases"] == {"runId": "run_id"}
        assert action["input_schema"]["required"] == ["run_id"]
        assert "runId" in action["input_schema"]["properties"]

    def test_list_issues_accepts_a_bounded_limit(self):
        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        action = next(a for a in manifest.actions if a["name"] == "list_issues")
        limit = action["input_schema"]["properties"]["limit"]
        assert limit["type"] == "integer"
        assert limit["minimum"] == 1
        assert limit["maximum"] == 1000


class TestPaperclipGetIssueWireLevel:
    def test_get_issue_with_canonical_issue_id(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"id": "STA-559", "title": "Example"}, calls)

        result = engine.execute(
            "get_issue", {"issue_id": "STA-559"}, cred(), CONFIG_JSON, session=None
        )

        assert result["success"] is True
        assert calls[0]["url"] == "https://paperclip.example.com/api/issues/STA-559"


class TestPaperclipListIssuesWireLevel:
    def test_list_issues_forwards_limit(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"issues": []}, calls)

        result = engine.execute(
            "list_issues", {"company_id": "company-1", "limit": 3}, cred(), CONFIG_JSON
        )

        assert result["success"] is True
        assert calls[0]["url"] == (
            "https://paperclip.example.com/api/companies/company-1/issues?limit=3"
        )

    def test_list_issues_enforces_limit_when_paperclip_ignores_it(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, [{"id": "first"}, {"id": "second"}], calls)

        result = engine.execute(
            "list_issues", {"company_id": "company-1", "limit": 1}, cred(), CONFIG_JSON
        )

        assert result["success"] is True
        assert result["body"] == [{"id": "first"}]
        assert result["limit_applied_locally"] is True
        assert "second" not in result["body_preview"]

    def test_get_issue_with_aliased_issue_id(self):
        engine = make_engine()
        calls: list = []
        mock_response(engine, {"id": "STA-559", "title": "Example"}, calls)

        # HttpEngine itself doesn't apply param_aliases (that's
        # connector_service._prepare_action_params, exercised at the
        # execute_binding_action layer in test_connector_action_params.py).
        # This confirms the render path works once the canonical key is present,
        # regardless of which name the caller originally used.
        result = engine.execute(
            "get_issue", {"issue_id": "STA-559"}, cred(), CONFIG_JSON, session=None
        )

        assert result["success"] is True
        assert calls[0]["url"] == "https://paperclip.example.com/api/issues/STA-559"


class TestPaperclipGetRunWireLevel:
    def test_get_run_with_canonical_run_id(self):
        engine = make_engine()
        calls: list = []
        mock_response(
            engine,
            {"id": "aaa9b76c-690f-4836-addc-a6c2e8ce1be6", "status": "succeeded"},
            calls,
        )

        result = engine.execute(
            "get_run",
            {"run_id": "aaa9b76c-690f-4836-addc-a6c2e8ce1be6"},
            cred(),
            CONFIG_JSON,
            session=None,
        )

        assert result["success"] is True
        assert (
            calls[0]["url"]
            == "https://paperclip.example.com/api/heartbeat-runs/aaa9b76c-690f-4836-addc-a6c2e8ce1be6"
        )


class TestPaperclipGetRunThroughDispatch:
    """Exercises the real manifest through execute_binding_action, the same
    layer connectors_run calls in production, so the alias fix is verified at
    the layer where the original incident actually occurred (not just against
    a synthetic connector_type as in test_connector_action_params.py)."""

    def test_get_run_alias_resolves_through_real_manifest(self, monkeypatch):
        import app.services.connector_service as svc

        manifest, err = load_and_validate(ADAPTER_PATH)
        assert err is None
        row = manifest.to_connector_type_row()
        connector_type = {
            "id": "paperclip",
            "auth_type": row["auth_type"],
            "provider_type": row["provider_type"],
            "operations_json": None,
            "endpoint_url": None,
            "backend_type": row["backend_type"],
            "backend_json": row["backend_json"],
            "supported_actions": manifest.actions,
            "disabled_actions": [],
        }
        binding = {
            "id": "binding-1",
            "enabled": True,
            "connector_type_id": "paperclip",
            "rate_limit_config_json": None,
            "config_json": CONFIG_JSON,
        }

        captured_params = {}
        monkeypatch.setattr(svc, "get_binding", lambda binding_id: binding)
        monkeypatch.setattr(svc, "get_connector_type", lambda ct_id: connector_type)
        monkeypatch.setattr(svc, "_check_rate_limit", lambda binding: None)
        monkeypatch.setattr(
            svc, "get_binding_with_credential", lambda binding_id: {"credential": cred()}
        )

        class FakeExecutor:
            needs_session = False

            def execute(self, action, params, credential, config_json, session=None):
                captured_params.update(params)
                return {"success": True, "body": {"id": params.get("run_id")}}

        monkeypatch.setattr(svc, "_resolve_executor", lambda ct: FakeExecutor())

        result = svc.execute_binding_action(
            "binding-1",
            "get_run",
            {"runId": "aaa9b76c-690f-4836-addc-a6c2e8ce1be6"},
        )

        assert result["success"] is True
        assert captured_params["run_id"] == "aaa9b76c-690f-4836-addc-a6c2e8ce1be6"
