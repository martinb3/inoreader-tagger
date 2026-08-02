"""Bridges the tagging engine to the database.

One place decides how a run is recorded, so the scheduler and the "Run now"
button in the UI cannot drift apart in what they persist.
"""

import logging
import threading
from typing import Optional

from .api import InoreaderAPI
from .crypto import TokenDecryptionError
from .db import (
    STATUS_AUTH_REQUIRED,
    STATUS_FAILED,
    Run,
    User,
    prune_runs,
    utcnow,
)
from .tagger import RunParameters, TaggerEngine

logger = logging.getLogger(__name__)


class RunService:
    """Executes tagging runs and records their outcome."""

    def __init__(self, settings, database, cipher):
        self.settings = settings
        self.database = database
        self.cipher = cipher
        # Guards against a scheduled run and a manual run overlapping for the
        # same account, which would double-apply tags and race the timestamp.
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, user_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def is_running(self, user_id: int) -> bool:
        return self._lock_for(user_id).locked()

    def execute(self, user_id: int, manual: bool = False) -> Optional[int]:
        """Run the tagger for one user. Returns the Run id, or None if skipped."""
        lock = self._lock_for(user_id)

        if not lock.acquire(blocking=False):
            logger.info("Run for user %s skipped: another run is in flight", user_id)
            return None

        try:
            return self._execute_locked(user_id, manual)
        finally:
            lock.release()

    def _execute_locked(self, user_id: int, manual: bool) -> Optional[int]:
        session = self.database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                logger.warning("Run requested for unknown user %s", user_id)
                return None

            run = Run(user_id=user.id, triggered_manually=manual, started_at=utcnow())
            session.add(run)
            session.commit()
            run_id = run.id

            api, setup_error = self._build_api(user)

            if api is None:
                run.status = STATUS_AUTH_REQUIRED
                run.error_message = setup_error
                run.log = setup_error
                run.finished_at = utcnow()
                user.needs_reauth = True
                session.commit()
                return run_id

            params = RunParameters(
                rules=user.rules,
                max_articles=user.max_articles,
                batch_size=user.batch_size,
                folder_filter=user.folder_filter,
                dry_run=user.dry_run,
                since_timestamp=user.last_processed_timestamp,
            )

            outcome = TaggerEngine(api, params).run()

            run.status = outcome.status
            run.processed = outcome.processed
            run.tagged = outcome.tagged
            run.skipped = outcome.skipped
            run.errors = outcome.errors
            run.error_message = outcome.error_message
            run.log = outcome.log
            run.finished_at = utcnow()

            if outcome.status == STATUS_AUTH_REQUIRED:
                user.needs_reauth = True
            else:
                user.needs_reauth = False
                if outcome.new_timestamp:
                    user.last_processed_timestamp = outcome.new_timestamp
                # Inoreader can rotate the refresh token during a refresh; if
                # it did, persist the new one or the next run will fail.
                if api.refresh_token and api.refresh_token != self._current_token(user):
                    user.refresh_token_encrypted = self.cipher.encrypt(api.refresh_token)

            prune_runs(session, user.id, self.settings.run_history_limit)
            session.commit()
            return run_id

        except Exception:  # noqa: BLE001 - never let a run kill the scheduler thread
            logger.exception("Failed to record run for user %s", user_id)
            session.rollback()
            self._mark_failed(user_id)
            return None
        finally:
            session.close()

    def _current_token(self, user: User) -> Optional[str]:
        if not user.refresh_token_encrypted:
            return None
        try:
            return self.cipher.decrypt(user.refresh_token_encrypted)
        except TokenDecryptionError:
            return None

    def _build_api(self, user: User):
        """Return (api, error). api is None when the user cannot be authenticated."""
        if not self.settings.oauth_configured:
            return None, (
                "INOREADER_APP_ID / INOREADER_APP_KEY are not configured on the server."
            )

        if not user.refresh_token_encrypted:
            return None, "No Inoreader connection stored — connect the account first."

        try:
            refresh_token = self.cipher.decrypt(user.refresh_token_encrypted)
        except TokenDecryptionError as exc:
            return None, str(exc)

        api = InoreaderAPI(
            app_id=self.settings.app_id,
            app_key=self.settings.app_key,
            refresh_token=refresh_token,
            redirect_uri=self.settings.redirect_uri,
        )
        return api, None

    def _mark_failed(self, user_id: int) -> None:
        """Best-effort record that a run blew up while being written."""
        session = self.database.session()
        try:
            run = Run(
                user_id=user_id,
                status=STATUS_FAILED,
                started_at=utcnow(),
                finished_at=utcnow(),
                error_message="Internal error while recording the run; see server logs.",
            )
            session.add(run)
            session.commit()
        except Exception:  # pragma: no cover - the database itself is unhappy
            logger.exception("Could not even record the failure for user %s", user_id)
            session.rollback()
        finally:
            session.close()
