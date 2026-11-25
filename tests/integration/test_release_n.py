"""Release N: the Health card and the Capabilities table say the same thing about failing connections."""

from app.database import get_db
from app.services import connector_service
from tests.integration.test_ui_truth import _bound_service, _services_tile


def _runs(binding_id, action, successes, failures):
    with get_db() as conn:
        for i in range(successes + failures):
            conn.execute(
                "INSERT INTO connector_executions (id, binding_id, action, result_status, executor_agent_id, executed_at) "
                "VALUES (?, ?, ?, ?, 'admin', datetime('now'))",
                (f"run-{action}-{i}", binding_id, action, "success" if i < successes else "error"),
            )
        conn.commit()


def test_health_does_not_say_all_working_while_an_action_keeps_failing(test_client, admin_token):
    binding = _bound_service()
    connector_service.update_binding(binding["id"], last_tested_at="2026-11-13T10:00:00+00:00", last_error=None)
    assert "all working" in _services_tile(test_client, admin_token)
    _runs(binding["id"], "GET /x", successes=0, failures=7)  # the check passed, every run failed
    assert connector_service.failing_actions_by_binding().get(binding["id"])
    tile = _services_tile(test_client, admin_token)
    assert "1 failing" in tile and "all working" not in tile


def test_a_connection_that_passes_its_check_and_runs_fine_is_all_working(test_client, admin_token):
    binding = _bound_service()
    connector_service.update_binding(binding["id"], last_tested_at="2026-11-13T10:00:00+00:00", last_error=None)
    _runs(binding["id"], "GET /x", successes=9, failures=0)
    assert "all working" in _services_tile(test_client, admin_token)


def test_a_connection_failing_in_both_ways_counts_once(test_client, admin_token):
    binding = _bound_service()
    connector_service.update_binding(binding["id"], last_tested_at="2026-11-13T10:00:00+00:00", last_error="refused")
    _runs(binding["id"], "GET /x", successes=0, failures=6)
    assert "1 failing" in _services_tile(test_client, admin_token)
