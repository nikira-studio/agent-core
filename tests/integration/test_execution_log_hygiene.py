from app.database import get_db
from app.services import backup_service, connector_service


def _binding(name="Test Binding"):
    connector_type = connector_service.list_connector_types()[0]
    return connector_service.create_binding(
        connector_type_id=connector_type["id"],
        scope="user:admin",
        name=name,
        credential_id=None,
        config_json="{}",
        created_by="admin",
    )


def _log(binding_id, action, status="success", body=None, age_days=0):
    execution_id = connector_service.log_execution(
        binding_id=binding_id,
        action=action,
        params_json="{}",
        result_status=status,
        result_body_json=body,
    )
    if age_days:
        with get_db() as conn:
            conn.execute(
                "UPDATE connector_executions SET executed_at = datetime('now', ?) WHERE id = ?",
                (f"-{age_days} days", execution_id),
            )
            conn.commit()
    return execution_id


def _stored_body(execution_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT result_body_json FROM connector_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
    return row["result_body_json"] if row else None


# --- write-time cap --------------------------------------------------------


def test_large_response_bodies_are_truncated(clean_db):
    binding = _binding()
    execution_id = _log(binding["id"], "list_issues", body="x" * 500_000)

    stored = _stored_body(execution_id)
    assert len(stored) < 20_000, "a 500 KB response must not be stored whole"
    assert "execution log truncated" in stored
    assert stored.startswith("xxx"), "the head of the response is what is worth keeping"


def test_small_bodies_are_stored_untouched(clean_db):
    binding = _binding()
    body = '{"ok": true, "items": []}'
    execution_id = _log(binding["id"], "get_me", body=body)
    assert _stored_body(execution_id) == body


def test_missing_body_stays_none(clean_db):
    binding = _binding()
    assert _stored_body(_log(binding["id"], "ping")) is None


# --- retention -------------------------------------------------------------


def test_maintenance_prunes_old_executions(clean_db):
    binding = _binding()
    recent = _log(binding["id"], "get_me", age_days=2)
    old = _log(binding["id"], "get_me", age_days=90)

    result = backup_service.run_scheduled_maintenance(triggered_by="test")
    assert result["executions_pruned"] == 1

    with get_db() as conn:
        remaining = {
            row["id"] for row in conn.execute("SELECT id FROM connector_executions")
        }
    assert recent in remaining
    assert old not in remaining


def test_retention_window_is_configurable(clean_db):
    binding = _binding()
    _log(binding["id"], "get_me", age_days=10)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('execution_log_retention_days', '5')"
        )
        conn.commit()

    assert backup_service.run_scheduled_maintenance(triggered_by="test")["executions_pruned"] == 1


def test_retention_of_zero_keeps_everything(clean_db):
    binding = _binding()
    _log(binding["id"], "get_me", age_days=400)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('execution_log_retention_days', '0')"
        )
        conn.commit()

    assert backup_service.run_scheduled_maintenance(triggered_by="test")["executions_pruned"] == 0


def test_pruning_is_audited(clean_db):
    import json

    binding = _binding()
    _log(binding["id"], "get_me", age_days=90)
    backup_service.run_scheduled_maintenance(triggered_by="test")

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'operational_logs_pruned' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert json.loads(row["details_json"])["connector_executions"] == 1


# --- per-action health -----------------------------------------------------


def test_action_health_reports_failure_rates(clean_db):
    binding = _binding()
    for _ in range(7):
        _log(binding["id"], "list_issues", status="error")
    for _ in range(3):
        _log(binding["id"], "list_issues", status="success")
    for _ in range(4):
        _log(binding["id"], "get_me", status="success")

    health = {stat["action"]: stat for stat in connector_service.action_health(binding["id"])}
    assert health["list_issues"]["calls"] == 10
    assert health["list_issues"]["failures"] == 7
    assert health["list_issues"]["failure_rate"] == 0.7
    assert health["get_me"]["failure_rate"] == 0.0


def test_action_health_reports_caller_validation_separately(clean_db):
    binding = _binding()
    message = "Invalid parameters: '3' is not of type 'integer'"
    for _ in range(5):
        connector_service.log_execution(
            binding_id=binding["id"], action="list_issues", params_json="{}",
            result_status="failure", error_message=message,
            error_code="INVALID_REQUEST",
            failure_category=connector_service.classify_failure("INVALID_REQUEST", message),
        )

    health = connector_service.action_health(binding["id"])
    assert health[0]["failure_categories"] == ["caller_validation"]


def test_action_health_ignores_calls_outside_the_window(clean_db):
    binding = _binding()
    _log(binding["id"], "list_issues", status="error", age_days=90)
    _log(binding["id"], "list_issues", status="success")

    health = connector_service.action_health(binding["id"], days=30)
    assert health[0]["calls"] == 1
    assert health[0]["failures"] == 0


def test_a_failing_action_shows_up_on_a_healthy_binding(clean_db):
    """A probe proves the connection works; it says nothing about the actions."""
    binding = _binding()
    connector_service.update_binding(
        binding["id"], last_tested_at="2026-07-27T00:00:00+00:00", last_error=None
    )
    for _ in range(6):
        _log(binding["id"], "add_comment", status="error")

    from app.security.scope_enforcer import ScopeEnforcer

    enforcer = ScopeEnforcer(["user:admin"], ["user:admin"], "admin", is_admin=True)
    summary = connector_service.build_capability_summary(enforcer, enabled_only=False)

    bindings = [b for c in summary["connectors"] for b in c["bindings"]]
    target = next(b for b in bindings if b["id"] == binding["id"])
    assert target["health"]["test_status"] == "passed"
    failing = target["health"]["failing_actions"]
    assert [f["action"] for f in failing] == ["add_comment"]
    assert failing[0]["failure_rate"] == 1.0


def test_occasional_failures_are_not_reported_as_broken(clean_db):
    binding = _binding()
    _log(binding["id"], "get_me", status="error")
    _log(binding["id"], "get_me", status="success")

    from app.security.scope_enforcer import ScopeEnforcer

    enforcer = ScopeEnforcer(["user:admin"], ["user:admin"], "admin", is_admin=True)
    summary = connector_service.build_capability_summary(enforcer, enabled_only=False)
    bindings = [b for c in summary["connectors"] for b in c["bindings"]]
    target = next(b for b in bindings if b["id"] == binding["id"])
    # 1 of 2 is a 50% failure rate but only 2 calls — too little to call it.
    assert target["health"]["failing_actions"] == []


# --- settings --------------------------------------------------------------


def test_retention_knobs_are_configurable_from_settings(test_client, admin_token):
    r = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scratchpad_retention_days": "7",
            "solo_mode_enabled": "false",
            "execution_log_retention_days": "14",
            "webhook_log_retention_days": "0",
        },
    )
    assert r.status_code == 200, r.json()
    saved = r.json()["data"]["settings"]
    assert saved["execution_log_retention_days"] == "14"
    assert saved["webhook_log_retention_days"] == "0"


def test_out_of_range_retention_is_rejected(test_client, admin_token):
    r = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scratchpad_retention_days": "7",
            "solo_mode_enabled": "false",
            "execution_log_retention_days": "9999",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_LOG_RETENTION"


def test_settings_page_renders_the_retention_controls(test_client, admin_token):
    r = test_client.get("/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "execution-log-retention-days" in r.text
    assert "webhook-log-retention-days" in r.text
