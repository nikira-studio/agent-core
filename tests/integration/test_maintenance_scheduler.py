"""Verifies the maintenance scheduler is actually wired into app startup/shutdown
(app.main's lifespan), not just correct in isolation (see
tests/unit/test_scheduler_service.py for the scheduler module itself)."""

from fastapi.testclient import TestClient


def test_app_lifespan_starts_and_cleanly_stops_maintenance_task(monkeypatch, clean_db):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 1)
    # Long initial delay so the real maintenance sweep never actually fires
    # mid-test; this test is about lifecycle wiring, not the sweep itself.
    monkeypatch.setattr(settings, "MAINTENANCE_INITIAL_DELAY_SECONDS", 3600)

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        task = app.state.maintenance_task
        assert task is not None
        assert not task.done()

    assert task.done()
    assert task.cancelled()


def test_app_lifespan_skips_maintenance_task_when_disabled(monkeypatch, clean_db):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 0)

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert app.state.maintenance_task is None
