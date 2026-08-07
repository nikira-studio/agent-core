"""In-process periodic maintenance scheduler.

Wired into app startup/shutdown (see app.main's lifespan) so every deployment
gets automatic scratchpad/TTL cleanup out of the box — no external cron job to
set up, and nothing an operator can forget to wire up. Without this, nothing
prunes stale scratchpad memories or TTL'd records unless an admin manually
clicks "Run Maintenance" in the dashboard.

Split into small, independently-testable pieces (_run_once, _maintenance_loop,
start/stop) so the loop behavior can be verified without real wall-clock
sleeps.
"""

import asyncio
import logging

from app.config import settings

logger = logging.getLogger(__name__)


async def _run_once() -> None:
    """Run one maintenance pass. Never raises — a single failed run (e.g. a
    transient DB lock) must not kill the loop for the rest of the process
    lifetime; it just retries on the next interval.

    The supported deployment is a single worker (see Dockerfile), but an
    operator can raise AGENT_CORE_WORKERS, and every process would then start
    its own copy of this scheduler. try_acquire_maintenance_lock takes a lease
    in the database so only one of them runs the sweep per tick; the rest skip
    silently rather than doing redundant (if harmless) work.
    """
    from app.services import backup_service

    try:
        acquired = await asyncio.to_thread(backup_service.try_acquire_maintenance_lock)
        if not acquired:
            logger.debug("Skipping scheduled maintenance run: another worker holds the lock")
            return
        result = await asyncio.to_thread(
            backup_service.run_scheduled_maintenance, triggered_by="scheduled"
        )
        logger.info("Scheduled maintenance run complete: %s", result)
    except Exception:
        logger.exception("Scheduled maintenance run failed; will retry next interval")


async def _maintenance_loop(initial_delay_seconds: float, interval_seconds: float) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        await _run_once()
        await asyncio.sleep(interval_seconds)


def start_maintenance_scheduler() -> asyncio.Task | None:
    """Start the background maintenance loop as an asyncio task.

    Returns None (no task started) when MAINTENANCE_INTERVAL_MINUTES <= 0, the
    documented way to disable the automatic scheduler while keeping manual
    "Run Maintenance" available.
    """
    interval_minutes = settings.MAINTENANCE_INTERVAL_MINUTES
    if interval_minutes <= 0:
        logger.info("Scheduled maintenance disabled (MAINTENANCE_INTERVAL_MINUTES <= 0)")
        return None

    interval_seconds = interval_minutes * 60
    initial_delay_seconds = settings.MAINTENANCE_INITIAL_DELAY_SECONDS
    logger.info(
        "Starting scheduled maintenance: every %d minute(s), first run in %d second(s)",
        interval_minutes,
        initial_delay_seconds,
    )
    return asyncio.create_task(_maintenance_loop(initial_delay_seconds, interval_seconds))


async def stop_maintenance_scheduler(task: asyncio.Task | None) -> None:
    """Cleanly cancel and await the scheduler task. No-op if none was started."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
