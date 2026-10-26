import importlib.util
import json
from pathlib import Path

from app.services import audit_service, memory_service


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "value_report.py"
    spec = importlib.util.spec_from_file_location("value_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(action, actor="agent-a", resource_id=None, **details):
    audit_service.write_event(
        actor_type="agent",
        actor_id=actor,
        action=action,
        resource_type="test",
        resource_id=resource_id,
        result="success",
        details=details,
    )


def test_report_pairs_fetches_with_offloads_and_flags_empty_agents(clean_db):
    value_report = _load_script()

    record, error = memory_service.write_memory(
        "Reported fact", "fact", "workspace:report", topic="report"
    )
    assert error is None
    memory_service.record_recall([record["id"]])

    _event("tool_result_offloaded", resource_id="read", tool="memory_search", total_chars=9000)
    _event("tool_result_fetched", resource_id="read", tool="memory_search",
           offset=0, returned_chars=9000, total_chars=9000, has_more=False)
    _event("tool_result_offloaded", resource_id="unread", tool="memory_search", total_chars=9000)
    for _ in range(5):
        _event("memory_search", actor="agent-empty", query="x", results=0)
    _event("memory_search", query="x", results=1)

    _event("context_injected", memory_ids=[record["id"], "other"], chars=500)
    _event("memory_fetched", ids=[record["id"]])

    report = value_report.build_report(clean_db, days=30)

    injected = report["injection"]["agents"]["agent-a"]
    assert injected["sessions"] == 1 and injected["records_listed"] == 2
    assert injected["fetched_pct"] == 50.0
    tool = report["delivery"]["tools"]["memory_search"]
    assert tool["offloaded"] == 2
    assert tool["read_through_pct"] == 50.0
    assert tool["fully_read_pct"] == 50.0
    assert report["search"]["no_value_agents"] == ["agent-empty"]
    scope = next(s for s in report["corpus"]["scopes"] if s["scope"] == "workspace:report")
    assert scope["never_recalled_pct"] == 0.0
    assert report["sync"]["by_type"]["memory_written"] == 100.0

    rendered = value_report.render(report)
    assert "agent-empty" in rendered
    json.dumps(report)


def test_offloads_before_fetch_tracking_are_untracked_not_unread(clean_db):
    value_report = _load_script()
    _event("tool_result_offloaded", resource_id="old", tool="briefing_list", total_chars=9000)

    report = value_report.build_report(clean_db, days=30)

    tool = report["delivery"]["tools"]["briefing_list"]
    assert report["delivery"]["fetch_tracking_since"] is None
    assert tool["tracked"] == 0
    assert tool["read_through_pct"] is None
