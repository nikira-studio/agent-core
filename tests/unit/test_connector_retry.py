import pytest
from app.services.connector_service import _is_transient_result


# --- _is_transient_result classification ---

def test_success_is_not_transient():
    assert _is_transient_result({"success": True}) is False


def test_429_is_transient():
    assert _is_transient_result({"success": False, "status": 429}) is True


def test_500_is_transient():
    assert _is_transient_result({"success": False, "status": 500}) is True


def test_503_is_transient():
    assert _is_transient_result({"success": False, "status": 503}) is True


def test_400_is_not_transient():
    assert _is_transient_result({"success": False, "status": 400}) is False


def test_404_is_not_transient():
    assert _is_transient_result({"success": False, "status": 404}) is False


def test_non_transient_error_codes():
    for code in ("NOT_FOUND", "DISABLED", "INVALID_ACTION", "NO_CREDENTIAL",
                 "RATE_LIMITED", "INVALID_CONFIGURATION", "SCOPE_DENIED"):
        assert _is_transient_result({"success": False, "error_code": code}) is False, code


def test_execution_error_with_timeout_is_transient():
    assert _is_transient_result({
        "success": False,
        "error_code": "EXECUTION_ERROR",
        "error": "Connection timed out",
    }) is True


def test_execution_error_with_connection_is_transient():
    assert _is_transient_result({
        "success": False,
        "error_code": "EXECUTION_ERROR",
        "error": "connection refused",
    }) is True


def test_execution_error_generic_is_not_transient():
    assert _is_transient_result({
        "success": False,
        "error_code": "EXECUTION_ERROR",
        "error": "unexpected key in response body",
    }) is False


def test_empty_result_is_not_transient():
    assert _is_transient_result({"success": False}) is False


# --- retry behavior via execute_binding_action ---

def test_retry_fires_on_transient_and_stops_on_success(monkeypatch):
    import app.services.connector_service as svc

    call_count = [0]

    def fake_get_binding(binding_id):
        return {
            "id": binding_id,
            "enabled": True,
            "connector_type_id": "ct1",
            "rate_limit_config_json": '{"max_retries": 2, "retry_delay_ms": 10}',
            "config_json": None,
        }

    def fake_get_connector_type(ct_id):
        return {"id": ct_id, "auth_type": "none", "provider_type": "openapi",
                "operations_json": None, "endpoint_url": None}

    def fake_validate(ct, action):
        return None

    def fake_check_rate_limit(binding):
        return None

    def fake_get_binding_with_credential(binding_id):
        return {"credential_plaintext": None}

    def fake_resolve_executor(ct):
        class FakeExecutor:
            def execute(self, action, params, credential, config_json):
                call_count[0] += 1
                if call_count[0] < 3:
                    return {"success": False, "status": 503, "error": "unavailable", "error_code": "EXECUTION_ERROR"}
                return {"success": True, "body": "ok"}
        return FakeExecutor()

    monkeypatch.setattr(svc, "get_binding", fake_get_binding)
    monkeypatch.setattr(svc, "get_connector_type", fake_get_connector_type)
    monkeypatch.setattr(svc, "_validate_action_for_connector", fake_validate)
    monkeypatch.setattr(svc, "_check_rate_limit", fake_check_rate_limit)
    monkeypatch.setattr(svc, "get_binding_with_credential", fake_get_binding_with_credential)
    monkeypatch.setattr(svc, "_resolve_executor", fake_resolve_executor)
    monkeypatch.setattr(svc, "_build_executor_config", lambda b, ct: {})
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)

    result = svc.execute_binding_action("b1", "some_action")
    assert result["success"] is True
    assert call_count[0] == 3


def test_non_transient_failure_is_not_retried(monkeypatch):
    import app.services.connector_service as svc

    call_count = [0]

    def fake_get_binding(binding_id):
        return {
            "id": binding_id,
            "enabled": True,
            "connector_type_id": "ct1",
            "rate_limit_config_json": '{"max_retries": 3, "retry_delay_ms": 10}',
            "config_json": None,
        }

    def fake_get_connector_type(ct_id):
        return {"id": ct_id, "auth_type": "none", "provider_type": "openapi",
                "operations_json": None, "endpoint_url": None}

    def fake_validate(ct, action):
        return None

    def fake_check_rate_limit(binding):
        return None

    def fake_get_binding_with_credential(binding_id):
        return {"credential_plaintext": None}

    def fake_resolve_executor(ct):
        class FakeExecutor:
            def execute(self, action, params, credential, config_json):
                call_count[0] += 1
                return {"success": False, "status": 404, "error": "not found", "error_code": "NOT_FOUND"}
        return FakeExecutor()

    monkeypatch.setattr(svc, "get_binding", fake_get_binding)
    monkeypatch.setattr(svc, "get_connector_type", fake_get_connector_type)
    monkeypatch.setattr(svc, "_validate_action_for_connector", fake_validate)
    monkeypatch.setattr(svc, "_check_rate_limit", fake_check_rate_limit)
    monkeypatch.setattr(svc, "get_binding_with_credential", fake_get_binding_with_credential)
    monkeypatch.setattr(svc, "_resolve_executor", fake_resolve_executor)
    monkeypatch.setattr(svc, "_build_executor_config", lambda b, ct: {})
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)

    result = svc.execute_binding_action("b1", "some_action")
    assert result["success"] is False
    assert call_count[0] == 1


def test_no_retry_when_max_retries_zero(monkeypatch):
    import app.services.connector_service as svc

    call_count = [0]

    def fake_get_binding(binding_id):
        return {
            "id": binding_id,
            "enabled": True,
            "connector_type_id": "ct1",
            "rate_limit_config_json": None,
            "config_json": None,
        }

    def fake_get_connector_type(ct_id):
        return {"id": ct_id, "auth_type": "none", "provider_type": "openapi",
                "operations_json": None, "endpoint_url": None}

    def fake_resolve_executor(ct):
        class FakeExecutor:
            def execute(self, action, params, credential, config_json):
                call_count[0] += 1
                return {"success": False, "status": 503, "error": "down", "error_code": "EXECUTION_ERROR"}
        return FakeExecutor()

    monkeypatch.setattr(svc, "get_binding", fake_get_binding)
    monkeypatch.setattr(svc, "get_connector_type", fake_get_connector_type)
    monkeypatch.setattr(svc, "_validate_action_for_connector", lambda ct, a: None)
    monkeypatch.setattr(svc, "_check_rate_limit", lambda b: None)
    monkeypatch.setattr(svc, "get_binding_with_credential", lambda bid: {"credential_plaintext": None})
    monkeypatch.setattr(svc, "_resolve_executor", fake_resolve_executor)
    monkeypatch.setattr(svc, "_build_executor_config", lambda b, ct: {})
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)

    result = svc.execute_binding_action("b1", "some_action")
    assert result["success"] is False
    assert call_count[0] == 1
