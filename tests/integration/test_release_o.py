"""Release O: an action is "failing" only while it has failed lately; older failures are history."""

from app.database import get_db
from app.services import connector_service
from tests.integration.test_ui_truth import _admin, _bound_service, _services_tile


def _run(binding_id, action, status, days_ago, message="", n=[0]):
    n[0] += 1
    with get_db() as conn:
        conn.execute(
            "INSERT INTO connector_executions (id, binding_id, action, result_status, error_message, executor_agent_id, executed_at) "
            "VALUES (?, ?, ?, ?, ?, 'admin', datetime('now', ?))",
            (f"o-{n[0]}", binding_id, action, status, message, f"-{days_ago} days"),
        )
        conn.commit()


def _run_coded(binding_id, action, code, message, days_ago):
    _run(binding_id, action, "failure", days_ago, message=message)
    with get_db() as conn:
        conn.execute("UPDATE connector_executions SET error_code = ? WHERE id = (SELECT MAX(id) FROM connector_executions WHERE binding_id = ?)", (code, binding_id))
        conn.commit()


def _failing(binding_id):
    return [s["action"] for s in connector_service.failing_actions_by_binding().get(binding_id, [])]


def _past(binding_id):
    return [s["action"] for s in connector_service.past_failures_by_binding().get(binding_id, [])]


def test_old_failures_followed_by_successes_are_not_failing(test_client, admin_token):
    binding = _bound_service()
    for i in range(7):
        _run(binding["id"], "GET /x", "failure", 20)
    for i in range(3):
        _run(binding["id"], "GET /x", "success", 2)
    assert _failing(binding["id"]) == []


def test_old_failures_with_no_runs_in_seven_days_are_history_not_failing(test_client, admin_token):
    binding = _bound_service()
    for i in range(7):
        _run(binding["id"], "GET /x", "failure", 20)  # 7 of 7, nothing since
    assert _failing(binding["id"]) == []
    assert _past(binding["id"]) == ["GET /x"]


def test_a_latest_run_that_failed_lately_is_failing_even_with_a_good_record(test_client, admin_token):
    binding = _bound_service()
    for i in range(10):
        _run(binding["id"], "GET /x", "success", 3)
    _run(binding["id"], "GET /x", "failure", 0)  # 1 of 11, but the latest run failed
    assert _failing(binding["id"]) == ["GET /x"]


def test_a_failure_this_week_that_meets_the_rate_rule_is_failing_even_if_the_latest_run_passed(test_client, admin_token):
    binding = _bound_service()
    for i in range(3):
        _run(binding["id"], "GET /x", "failure", 3)
    for i in range(3):
        _run(binding["id"], "GET /x", "success", 1)  # 3 of 6 failed, latest passed
    assert _failing(binding["id"]) == ["GET /x"]


def test_a_failure_this_week_below_the_rate_rule_with_a_passing_latest_run_is_not_failing(test_client, admin_token):
    binding = _bound_service()
    _run(binding["id"], "GET /x", "failure", 3)
    for i in range(9):
        _run(binding["id"], "GET /x", "success", 1)  # 1 of 10 failed, latest passed
    assert _failing(binding["id"]) == []


def test_a_caller_guessing_an_action_name_is_not_the_connection_failing(test_client, admin_token):
    binding = _bound_service()
    _run(binding["id"], "list_clients", "failure", 0, message="Unknown action: list_clients")
    assert _failing(binding["id"]) == [] and _past(binding["id"]) == []


def test_health_and_the_connections_table_agree_and_history_is_a_quiet_line(test_client, admin_token):
    binding = _bound_service()
    connector_service.update_binding(binding["id"], last_tested_at="2026-11-13T10:00:00+00:00", last_error=None)
    for i in range(7):
        _run(binding["id"], "GET /x", "failure", 20)
    assert "all working" in _services_tile(test_client, admin_token)
    html = test_client.get("/connectors", headers=_admin(admin_token)).text
    assert "GET /x last failed" in html and "failing 7/7" not in html
    _run(binding["id"], "GET /x", "failure", 0)  # now it failed again, today
    assert "1 failing" in _services_tile(test_client, admin_token)
    html = test_client.get("/connectors", headers=_admin(admin_token)).text
    assert "GET /x failing" in html and "last failed" not in html.split("GET /x failing", 1)[1].split("</td>", 1)[0]


def test_naming_an_action_the_connection_does_not_have_is_not_failing(test_client, admin_token):
    binding = _bound_service()
    _run_coded(binding["id"], "Search", "INVALID_ACTION", "Action not supported: Search", 1)
    _run_coded(binding["id"], "Wipe", "DISABLED_ACTION", "This action is switched off", 1)
    assert _failing(binding["id"]) == [] and _past(binding["id"]) == []
