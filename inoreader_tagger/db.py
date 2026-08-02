"""Persistence layer: users, their tagging rules, and their run history.

State that used to live in config.json and .last_processed_timestamp now lives
here, one row per user, so the service can run unattended for many accounts.
"""

import datetime as dt
import json
import logging
from typing import Iterator, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

logger = logging.getLogger(__name__)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


# Run outcomes. AUTH_REQUIRED is deliberately distinct from FAILED: it is the
# one status that needs a human to go and reconnect Inoreader, and the status
# page keys its "re-login" banner off it.
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_AUTH_REQUIRED = "auth_required"
STATUS_RUNNING = "running"

TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILED, STATUS_AUTH_REQUIRED)


class User(Base):
    """One connected Inoreader account."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity comes from Inoreader itself — there are no local passwords.
    inoreader_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(320), default=None)
    display_name: Mapped[Optional[str]] = mapped_column(String(320), default=None)

    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # True once a refresh has been rejected; cleared when the user reconnects.
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False)
    last_authenticated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    folder_filter: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    max_articles: Mapped[int] = mapped_column(Integer, default=200)
    batch_size: Mapped[int] = mapped_column(Integer, default=100)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    # Replaces the old .last_processed_timestamp file. Microseconds, as a
    # string, because Inoreader's timestampUsec exceeds 32-bit range and we
    # only ever compare it numerically.
    last_processed_timestamp: Mapped[Optional[str]] = mapped_column(String(32), default=None)

    # Tagging rules as a JSON array, same schema the CLI's config.json used.
    rules_json: Mapped[str] = mapped_column(Text, default="[]")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    runs: Mapped[List["Run"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="Run.started_at.desc()",
    )

    @property
    def rules(self) -> list:
        try:
            parsed = json.loads(self.rules_json or "[]")
        except json.JSONDecodeError:
            logger.error("User %s has unparseable rules_json; treating as empty", self.id)
            return []
        return parsed if isinstance(parsed, list) else []

    @rules.setter
    def rules(self, value: list) -> None:
        self.rules_json = json.dumps(value, indent=2)

    @property
    def label(self) -> str:
        return self.email or self.display_name or f"user {self.inoreader_user_id}"


class Run(Base):
    """One execution of the tagger for one user."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_RUNNING)

    # True when a person pressed "Run now" rather than the scheduler firing.
    triggered_manually: Mapped[bool] = mapped_column(Boolean, default=False)

    processed: Mapped[int] = mapped_column(Integer, default=0)
    tagged: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    log: Mapped[Optional[str]] = mapped_column(Text, default=None)

    user: Mapped[User] = relationship(back_populates="runs")

    @property
    def duration_seconds(self) -> Optional[float]:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, settings):
        self._settings = settings
        self.engine: Engine = self._make_engine(settings)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _make_engine(settings) -> Engine:
        url = settings.database_url
        kwargs = {"future": True, "pool_pre_ping": True}

        if url.startswith("sqlite"):
            # The scheduler and the web server share one process but different
            # threads, so the default same-thread check has to go.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

        engine = create_engine(url, **kwargs)

        if url.startswith("sqlite"):
            journal_mode = settings.sqlite_journal_mode

            @event.listens_for(engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                # WAL needs shared memory that NFS does not provide, so the
                # deployed default is TRUNCATE. Overridable for local dev.
                cursor.execute(f"PRAGMA journal_mode={journal_mode}")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        return engine

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self):
        return self._session_factory()


def prune_runs(session, user_id: int, keep: int) -> int:
    """Drop run records beyond the newest `keep` for a user. Returns count removed."""
    stale = session.scalars(
        select(Run)
        .where(Run.user_id == user_id)
        .order_by(Run.started_at.desc())
        .offset(keep)
    ).all()

    for run in stale:
        session.delete(run)
    return len(stale)
