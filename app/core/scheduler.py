"""Background jobs.

Three jobs. Closing punch sessions that were never punched out runs hourly and only
acts on days already past in the organisation's timezone, so a night shift in
progress is never touched. Crediting leave accrual runs once a day and credits
whatever cycles have turned since it last ran.

The scheduler runs in-process. With several workers each would run the jobs; both
are idempotent (a session already flagged ``auto_closed`` is skipped, an accrual
cycle already in the ledger is not credited again), so the duplication is
wasteful rather than wrong. Move to a single scheduled worker if this ever runs
at scale.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _close_stale_sessions_job() -> None:
    from app.services.attendance import close_stale_sessions

    try:
        closed = await close_stale_sessions()
        if closed:
            logger.info("Scheduled job closed %d stale attendance day(s).", closed)
    except Exception:  # noqa: BLE001 - a failed job must not kill the scheduler
        logger.exception("The stale-session job failed.")


async def _leave_accrual_job() -> None:
    from app.services.leave import run_accruals

    try:
        credited = await run_accruals()
        if credited:
            logger.info("Scheduled job credited %d leave balance(s).", credited)
    except Exception:  # noqa: BLE001 - a failed job must not kill the scheduler
        logger.exception("The leave accrual job failed.")


async def _weekly_digest_job() -> None:
    from app.services.notification import weekly_digest

    try:
        told = await weekly_digest()
        if told:
            logger.info("Weekly digest sent to %d administrator(s).", told)
    except Exception:  # noqa: BLE001 - a failed job must not kill the scheduler
        logger.exception("The weekly digest job failed.")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler(timezone=str(settings.org_tz))
    _scheduler.add_job(
        _close_stale_sessions_job,
        CronTrigger(minute=5),  # five past every hour
        id="close_stale_sessions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _leave_accrual_job,
        # Just after midnight, so a cycle that turns overnight is credited
        # before anyone looks at their balance in the morning.
        CronTrigger(hour=0, minute=20),
        id="leave_accrual",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _weekly_digest_job,
        # Monday morning, before the working day starts.
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started with %d job(s).", len(_scheduler.get_jobs()))


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("Scheduler stopped.")
