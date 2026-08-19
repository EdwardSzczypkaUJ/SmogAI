from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from smog_ai.config import AppConfig
from smog_ai.database.models import Base
from smog_ai.errors import DatabaseError


def create_db_engine(config: AppConfig) -> Engine:
    engine = create_engine(
        config.database_url,
        future=True,
        pool_pre_ping=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def init_database(engine: Engine, *, verify_integrity: bool = True) -> None:
    """Create missing schema objects and optionally verify the whole SQLite file.

    ``PRAGMA quick_check`` scans the complete database.  It is appropriate for
    an explicit database preflight, but running it in every short-lived CLI
    process made one Serving cycle scan the same multi-gigabyte database more
    than ten times.
    """

    try:
        Base.metadata.create_all(engine)
        if verify_integrity:
            with engine.begin() as connection:
                result = connection.execute(text("PRAGMA quick_check")).scalar_one()
                if result != "ok":
                    raise DatabaseError(f"SQLite quick_check returned: {result}")
    except Exception as exc:
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError(f"Cannot initialize database: {exc}") from exc


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextlib.contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
