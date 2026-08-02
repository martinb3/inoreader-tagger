"""Runtime configuration, sourced from environment variables.

Everything the service needs to boot is read here exactly once, so the rest of
the codebase never touches os.environ directly.
"""

import os
from dataclasses import dataclass
from typing import Optional


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Immutable view of the service configuration."""

    # Where persistent state lives. The encryption key and the default SQLite
    # database are both placed here, so mounting one volume is enough.
    data_dir: str

    # SQLAlchemy URL. Defaults to SQLite inside data_dir.
    database_url: str

    # SQLite journal mode. WAL requires shared memory that network filesystems
    # such as NFS do not provide, so the default is the portable TRUNCATE.
    # Override to WAL when the data directory is on a local disk.
    sqlite_journal_mode: str

    # Inoreader OAuth application credentials (one app, many users).
    app_id: Optional[str]
    app_key: Optional[str]

    # External URL the status page is reached at. Used to build the OAuth
    # redirect URI, which must match the one registered with Inoreader.
    public_base_url: str

    # Defaults applied to newly registered users.
    default_interval_minutes: int
    default_max_articles: int
    default_batch_size: int

    # How many run records to retain per user.
    run_history_limit: int

    listen_host: str
    listen_port: int
    log_level: str

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"

    @property
    def oauth_configured(self) -> bool:
        return bool(self.app_id and self.app_key)


def load_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR", "/data")
    default_db = f"sqlite:///{os.path.join(data_dir, 'inoreader-tagger.db')}"

    public_base_url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

    return Settings(
        data_dir=data_dir,
        database_url=os.environ.get("DATABASE_URL", default_db),
        sqlite_journal_mode=os.environ.get("SQLITE_JOURNAL_MODE", "TRUNCATE"),
        app_id=os.environ.get("INOREADER_APP_ID"),
        app_key=os.environ.get("INOREADER_APP_KEY"),
        public_base_url=public_base_url,
        default_interval_minutes=_int_env("DEFAULT_INTERVAL_MINUTES", 30),
        default_max_articles=_int_env("DEFAULT_MAX_ARTICLES", 200),
        default_batch_size=_int_env("DEFAULT_BATCH_SIZE", 100),
        run_history_limit=_int_env("RUN_HISTORY_LIMIT", 50),
        listen_host=os.environ.get("LISTEN_HOST", "0.0.0.0"),
        listen_port=_int_env("LISTEN_PORT", 8000),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
