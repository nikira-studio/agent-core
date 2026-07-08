import asyncio

import pytest

from app.services import scheduler_service


@pytest.mark.asyncio
async def test_run_once_invokes_maintenance_with_scheduled_trigger(monkeypatch):
    calls = []

    def fake_run_scheduled_maintenance(triggered_by="manual"):
        calls.append(triggered_by)
        return {"stale_activities_marked": 0, "scratchpad_pruned": 0, "ttl_swept": 0}

    import app.services.backup_service as backup_service

    monkeypatch.setattr(backup_service, "try_acquire_maintenance_lock", lambda: True)
    monkeypatch.setattr(
        backup_service, "run_scheduled_maintenance", fake_run_scheduled_maintenance
    )

    await scheduler_service._run_once()

    assert calls == ["scheduled"]


@pytest.mark.asyncio
async def test_run_once_skips_run_when_lock_not_acquired(monkeypatch):
    calls = []

    import app.services.backup_service as backup_service

    monkeypatch.setattr(backup_service, "try_acquire_maintenance_lock", lambda: False)
    monkeypatch.setattr(
        backup_service,
        "run_scheduled_maintenance",
        lambda triggered_by="manual": calls.append(triggered_by),
    )

    await scheduler_service._run_once()

    assert calls == [], "another worker holding the lock must prevent this run"


@pytest.mark.asyncio
async def test_run_once_swallows_exceptions(monkeypatch):
    import app.services.backup_service as backup_service

    def boom(triggered_by="manual"):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(backup_service, "try_acquire_maintenance_lock", lambda: True)
    monkeypatch.setattr(backup_service, "run_scheduled_maintenance", boom)

    # Must not raise -- a single failed run cannot kill the loop.
    await scheduler_service._run_once()


@pytest.mark.asyncio
async def test_run_once_swallows_exceptions_from_lock_acquisition(monkeypatch):
    import app.services.backup_service as backup_service

    def boom():
        raise RuntimeError("db is locked")

    monkeypatch.setattr(backup_service, "try_acquire_maintenance_lock", boom)

    # Must not raise even if the lock acquisition itself fails.
    await scheduler_service._run_once()


@pytest.mark.asyncio
async def test_maintenance_loop_runs_repeatedly_until_cancelled(monkeypatch):
    call_count = 0

    async def fake_run_once():
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(scheduler_service, "_run_once", fake_run_once)

    task = asyncio.create_task(
        scheduler_service._maintenance_loop(initial_delay_seconds=0, interval_seconds=0.01)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count >= 2, "loop should have run more than once in 0.1s at a 0.01s interval"


@pytest.mark.asyncio
async def test_maintenance_loop_respects_initial_delay(monkeypatch):
    call_count = 0

    async def fake_run_once():
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(scheduler_service, "_run_once", fake_run_once)

    task = asyncio.create_task(
        scheduler_service._maintenance_loop(initial_delay_seconds=1.0, interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count == 0, "no run should happen before the initial delay elapses"


def test_start_maintenance_scheduler_disabled_when_interval_zero(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 0)

    task = scheduler_service.start_maintenance_scheduler()

    assert task is None


@pytest.mark.asyncio
async def test_start_and_stop_maintenance_scheduler_lifecycle(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 1)
    monkeypatch.setattr(settings, "MAINTENANCE_INITIAL_DELAY_SECONDS", 10)

    task = scheduler_service.start_maintenance_scheduler()

    assert task is not None
    assert isinstance(task, asyncio.Task)
    assert not task.done()

    # Must cancel and await cleanly -- no exception should escape, and the
    # task must actually be finished afterward (proves shutdown is not a
    # fire-and-forget cancel that leaves the task dangling).
    await scheduler_service.stop_maintenance_scheduler(task)

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_stop_maintenance_scheduler_is_noop_for_none():
    # Must not raise when the scheduler was never started (disabled).
    await scheduler_service.stop_maintenance_scheduler(None)
