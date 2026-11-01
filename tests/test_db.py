"""Engine behaviour when SQLite reports errors, as it does during NFS stalls."""

import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from inoreader_tagger.db import Database


@pytest.fixture
def database(tmp_path):
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        sqlite_journal_mode="DELETE",
    )
    db = Database(settings)
    db.create_all()
    yield db
    db.engine.dispose()


def sqlite_error(message: str, errorname: str) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorname = errorname
    return exc


def fail_next_execute(monkeypatch, database, exc) -> None:
    """Make the next statement raise `exc` from the driver, then behave normally."""
    dialect = database.engine.dialect
    real_execute = dialect.do_execute
    state = {"raised": False}

    def do_execute(cursor, statement, parameters, context=None):
        if not state["raised"]:
            state["raised"] = True
            raise exc
        return real_execute(cursor, statement, parameters, context)

    monkeypatch.setattr(dialect, "do_execute", do_execute)


def driver_connection(database):
    with database.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        return conn.connection.dbapi_connection


def test_io_error_discards_the_pooled_connection(monkeypatch, database):
    before = driver_connection(database)
    fail_next_execute(monkeypatch, database, sqlite_error("disk I/O error", "SQLITE_IOERR"))

    with pytest.raises(OperationalError):
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    after = driver_connection(database)
    assert after is not before


def test_extended_io_error_codes_also_reconnect(monkeypatch, database):
    before = driver_connection(database)
    fail_next_execute(monkeypatch, database, sqlite_error("disk I/O error", "SQLITE_IOERR_READ"))

    with pytest.raises(OperationalError):
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    assert driver_connection(database) is not before


def test_busy_database_keeps_the_pooled_connection(monkeypatch, database):
    before = driver_connection(database)
    fail_next_execute(monkeypatch, database, sqlite_error("database is locked", "SQLITE_BUSY"))

    with pytest.raises(OperationalError):
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    assert driver_connection(database) is before
