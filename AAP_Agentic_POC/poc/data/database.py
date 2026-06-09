"""Engine, session factory, and schema lifecycle helpers for poc.db.

This is the single chokepoint through which every part of the POC opens the
SQLite database that mocks Palantir + Snowflake/AS400. It also installs the
append-only triggers that make ``audit_log`` immutable at the DB level, not
merely by convention.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import DB_URL
from data.models import Base

# A single shared engine. check_same_thread=False so Streamlit's threads can
# reuse it; future=True for SQLAlchemy 2.0 semantics.
ENGINE: Engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)


@event.listens_for(ENGINE, "connect")
def _enable_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Enable foreign keys on every SQLite connection.

    Args:
        dbapi_connection: Raw DBAPI connection handed over by the pool.
        _connection_record: Pool bookkeeping record (unused).

    Rationale: SQLite ignores FK constraints unless PRAGMA foreign_keys=ON is
    set per-connection, and we rely on FKs to keep vendor/SKU links honest.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# SQL that forbids mutation of audit_log rows once written. Append-only is a
# hard requirement: routing decisions, POs, and suppressions are evidence.
_AUDIT_IMMUTABLE_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not allowed');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not allowed');
    END;
    """,
)


def get_session() -> Session:
    """Open a new ORM session bound to the shared engine.

    Returns:
        A fresh ``Session``; the caller is responsible for closing it
        (use it as a context manager via ``with get_session() as s``).

    Rationale: one factory keeps session configuration consistent everywhere.
    """
    return SessionLocal()


def _install_audit_triggers() -> None:
    """Create the append-only triggers on audit_log (idempotent).

    Rationale: enforces immutability in the engine so a buggy agent cannot
    rewrite history even if it tries.
    """
    with ENGINE.begin() as conn:
        for ddl in _AUDIT_IMMUTABLE_TRIGGERS:
            conn.execute(text(ddl))


def init_db() -> None:
    """Create all tables and install audit triggers if absent (idempotent).

    Rationale: safe to call on every startup; does not drop existing data.
    """
    Base.metadata.create_all(ENGINE)
    _install_audit_triggers()


def reset_db() -> None:
    """Drop every table and recreate an empty schema from scratch.

    Rationale: the seed script needs a clean slate so the four scenarios are
    reproduced exactly, with no leftover rows from a prior run.
    """
    # Triggers reference audit_log; drop them first so the table can be dropped.
    with ENGINE.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS audit_log_no_update"))
        conn.execute(text("DROP TRIGGER IF EXISTS audit_log_no_delete"))
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    _install_audit_triggers()
