"""Per-user recurring runs.

Each enabled user gets one APScheduler job at their own interval. The job set
is reconciled from the database rather than mutated from request handlers, so
a restart, a config change, and a new signup all converge on the same state.
"""

import logging
from typing import List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .db import User

logger = logging.getLogger(__name__)

JOB_PREFIX = "tag-user-"
RECONCILE_JOB_ID = "reconcile-jobs"
RECONCILE_INTERVAL_SECONDS = 60


def _job_id(user_id: int) -> str:
    return f"{JOB_PREFIX}{user_id}"


class TaggerScheduler:
    """Owns the APScheduler instance and keeps its jobs in sync with the DB."""

    def __init__(self, settings, database, run_service):
        self.settings = settings
        self.database = database
        self.run_service = run_service
        self._scheduler = BackgroundScheduler(
            job_defaults={
                # A slow run must not stack up behind itself.
                "coalesce": True,
                "max_instances": 1,
                # Tolerate the pod being briefly descheduled without APScheduler
                # discarding the fire it missed.
                "misfire_grace_time": 300,
            },
            timezone="UTC",
        )

    def start(self) -> None:
        self._scheduler.add_job(
            self.reconcile,
            trigger=IntervalTrigger(seconds=RECONCILE_INTERVAL_SECONDS),
            id=RECONCILE_JOB_ID,
            replace_existing=True,
        )
        self._scheduler.start()
        self.reconcile()
        logger.info("Scheduler started")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def reconcile(self) -> None:
        """Make the live job set match the enabled, connected users in the DB."""
        session = self.database.session()
        try:
            users = session.scalars(select(User)).all()
        except Exception:  # noqa: BLE001 - a DB blip must not kill the scheduler
            logger.exception("Could not reconcile scheduler jobs")
            return
        finally:
            session.close()

        wanted = {}
        for user in users:
            # Users needing re-auth keep their job: the run records the
            # auth_required status, which is what drives the status page.
            if user.enabled and user.refresh_token_encrypted:
                wanted[_job_id(user.id)] = user

        existing = {
            job.id for job in self._scheduler.get_jobs() if job.id.startswith(JOB_PREFIX)
        }

        for job_id in existing - wanted.keys():
            self._scheduler.remove_job(job_id)
            logger.info("Removed schedule %s", job_id)

        for job_id, user in wanted.items():
            interval = max(1, user.interval_minutes)
            job = self._scheduler.get_job(job_id)
            trigger = IntervalTrigger(minutes=interval, timezone="UTC")

            if job is None:
                self._scheduler.add_job(
                    self.run_service.execute,
                    trigger=trigger,
                    id=job_id,
                    args=[user.id],
                    replace_existing=True,
                )
                logger.info("Scheduled user %s every %s minute(s)", user.id, interval)
            elif _interval_minutes(job) != interval:
                self._scheduler.reschedule_job(job_id, trigger=trigger)
                logger.info("Rescheduled user %s to every %s minute(s)", user.id, interval)

    def trigger_now(self, user_id: int) -> None:
        """Queue an immediate one-off run without blocking the caller."""
        self._scheduler.add_job(
            self.run_service.execute,
            id=f"manual-{user_id}",
            args=[user_id],
            kwargs={"manual": True},
            replace_existing=True,
            misfire_grace_time=60,
        )

    def next_run_time(self, user_id: int) -> Optional[object]:
        job = self._scheduler.get_job(_job_id(user_id))
        return job.next_run_time if job else None

    def jobs(self) -> List[object]:
        return self._scheduler.get_jobs()

    @property
    def running(self) -> bool:
        return self._scheduler.running


def _interval_minutes(job) -> Optional[int]:
    interval = getattr(job.trigger, "interval", None)
    if interval is None:
        return None
    return int(interval.total_seconds() // 60)
