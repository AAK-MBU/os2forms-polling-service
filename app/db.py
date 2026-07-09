"""Synchronous SQLAlchemy engine for the polling service's own SQL Server database.

Holds everything in the ``[polling]`` schema (PollJob, ApiKey, SubmissionPollStatus). A poller
processes jobs sequentially, so a synchronous engine + pyodbc is simpler and more robust than
async ODBC. Sessions are SQLModel ``Session``s so repositories can use ``.exec()``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session

from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def _to_sqlalchemy_url(database_url: str) -> str | URL:
    """Accept either a SQLAlchemy URL or a raw ODBC connection string.

    - SQLAlchemy URL:  ``mssql+pyodbc://user:pass@host:1433/RPA?driver=...`` (has ``://``)
    - ODBC DSN string: ``Driver={ODBC Driver 18 for SQL Server};Server=host,1433;Database=RPA;
      Uid=...;Pwd=...;TrustServerCertificate=yes;`` — wrapped into pyodbc's ``odbc_connect``.
    """
    s = database_url.strip()
    lowered = s.lower()
    if "://" not in s and ("driver=" in lowered or "server=" in lowered):
        return URL.create("mssql+pyodbc", query={"odbc_connect": s})
    return s


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        url = _to_sqlalchemy_url(settings.database_url)
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(
            bind=get_engine(), class_=Session, expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def bootstrap_state_table(settings: Settings | None = None) -> None:
    """Create the [polling] schema and its tables if missing (create-on-miss).

    Runs every migrations/*.sql in filename order, each as a single idempotent T-SQL batch
    (SQL Server executes multi-statement batches without GO; exec_driver_sql skips SQLAlchemy
    bind-param parsing). Order matters — 001 PollJob before 003's FK to it.
    """
    settings = settings or get_settings()
    if not settings.bootstrap_state_table:
        return
    engine = get_engine()
    for sql_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        ddl = sql_path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl)
        log.info("migration.applied", file=sql_path.name)
    log.info("state.bootstrapped", schema=settings.state_schema)
