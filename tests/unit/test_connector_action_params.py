import json


def _binding(config_json=None):
    return {
        "id": "binding-1",
        "enabled": True,
        "connector_type_id": "paperclip",
        "rate_limit_config_json": None,
        "config_json": config_json,
    }


def _connector_type(action_meta):
    return {
        "id": "paperclip",
        "auth_type": "none",
        "provider_type": "builtin",
        "operations_json": None,
        "endpoint_url": None,
        "supported_actions": [action_meta],
    }


def _patch_common(monkeypatch, svc, binding, connector_type, captured_params):
    monkeypatch.setattr(svc, "get_binding", lambda binding_id: binding)
    monkeypatch.setattr(svc, "get_connector_type", lambda ct_id: connector_type)
    monkeypatch.setattr(svc, "_check_rate_limit", lambda binding: None)
    monkeypatch.setattr(
        svc, "get_binding_with_credential", lambda binding_id: {"credential": None}
    )

    class FakeExecutor:
        needs_session = False

        def execute(self, action, params, credential, config_json, session=None):
            captured_params.update(params)
            return {"success": True, "body": {"params": params}}

    monkeypatch.setattr(svc, "_resolve_executor", lambda ct: FakeExecutor())


def test_execute_binding_action_applies_default_params_before_validation(monkeypatch):
    import app.services.connector_service as svc

    captured_params = {}
    binding = _binding(
        json.dumps({"default_params": {"company_id": "company-default"}})
    )
    connector_type = _connector_type(
        {
            "name": "list_issues",
            "input_schema": {
                "type": "object",
                "required": ["company_id"],
                "properties": {
                    "company_id": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        }
    )
    _patch_common(monkeypatch, svc, binding, connector_type, captured_params)

    result = svc.execute_binding_action(
        "binding-1", "list_issues", {"status": "in_progress"}
    )

    assert result["success"] is True
    assert captured_params == {
        "company_id": "company-default",
        "status": "in_progress",
    }


def test_execute_binding_action_caller_params_override_defaults(monkeypatch):
    import app.services.connector_service as svc

    captured_params = {}
    binding = _binding(
        json.dumps({"default_params": {"company_id": "company-default"}})
    )
    connector_type = _connector_type(
        {
            "name": "list_issues",
            "input_schema": {
                "type": "object",
                "required": ["company_id"],
                "properties": {"company_id": {"type": "string"}},
            },
        }
    )
    _patch_common(monkeypatch, svc, binding, connector_type, captured_params)

    result = svc.execute_binding_action(
        "binding-1", "list_issues", {"company_id": "company-explicit"}
    )

    assert result["success"] is True
    assert captured_params["company_id"] == "company-explicit"


def test_execute_binding_action_normalizes_declared_param_aliases(monkeypatch):
    import app.services.connector_service as svc

    captured_params = {}
    binding = _binding()
    connector_type = _connector_type(
        {
            "name": "get_issue",
            "param_aliases": {"issueId": "issue_id"},
            "input_schema": {
                "type": "object",
                "required": ["issue_id"],
                "properties": {
                    "issue_id": {"type": "string"},
                    "issueId": {"type": "string"},
                },
            },
        }
    )
    _patch_common(monkeypatch, svc, binding, connector_type, captured_params)

    result = svc.execute_binding_action(
        "binding-1", "get_issue", {"issueId": "STA-132"}
    )

    assert result["success"] is True
    assert captured_params["issue_id"] == "STA-132"


def test_execute_binding_action_infers_camel_case_alias_from_schema(monkeypatch):
    import app.services.connector_service as svc

    captured_params = {}
    binding = _binding()
    connector_type = _connector_type(
        {
            "name": "get_issue",
            "input_schema": {
                "type": "object",
                "required": ["issue_id"],
                "properties": {
                    "issue_id": {"type": "string"},
                },
            },
        }
    )
    _patch_common(monkeypatch, svc, binding, connector_type, captured_params)

    result = svc.execute_binding_action(
        "binding-1", "get_issue", {"issueId": "STA-132"}
    )

    assert result["success"] is True
    assert captured_params["issue_id"] == "STA-132"


def test_execute_binding_action_still_rejects_missing_required_param(monkeypatch):
    import app.services.connector_service as svc

    binding = _binding()
    connector_type = _connector_type(
        {
            "name": "list_issues",
            "input_schema": {
                "type": "object",
                "required": ["company_id"],
                "properties": {"company_id": {"type": "string"}},
            },
        }
    )
    monkeypatch.setattr(svc, "get_binding", lambda binding_id: binding)
    monkeypatch.setattr(svc, "get_connector_type", lambda ct_id: connector_type)

    result = svc.execute_binding_action("binding-1", "list_issues", {})

    assert result["success"] is False
    assert result["error_code"] == "INVALID_PARAMS"


def test_execute_binding_action_rejects_non_object_params(monkeypatch):
    import app.services.connector_service as svc

    binding = _binding()
    connector_type = _connector_type(
        {
            "name": "list_issues",
            "input_schema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        }
    )
    monkeypatch.setattr(svc, "get_binding", lambda binding_id: binding)
    monkeypatch.setattr(svc, "get_connector_type", lambda ct_id: connector_type)

    result = svc.execute_binding_action("binding-1", "list_issues", "bad")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_PARAMS"
    assert "params must be an object" in result["error"]
