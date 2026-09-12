"""Database connectivity: SQLite (relational) and MongoDB (documents).

SQLite holds anything with relationships and constraints -- users, students,
quizzes, progress. MongoDB holds schema-loose documents: parsed curriculum
chunks, agent run traces and generated-content caches.

MongoDB is optional. If ``MONGODB_URI`` is unset the app still boots and the
document-backed features degrade rather than crash, which keeps local
development possible with nothing but a SQLite file.

Usage in a route:
    from fastapi import Depends
    from sqlalchemy.orm import Session
    from db.database import get_db

    @router.get("/students/{student_id}")
    def read_student(student_id: str, db: Session = Depends(get_db)):
        ...
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from db.models import Base

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# SQLite / SQLAlchemy
# --------------------------------------------------------------------------- #

engine: Engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    echo=settings.SQL_ECHO,
    # FastAPI serves requests from a threadpool; SQLite's default thread guard
    # would reject connections reused across threads.
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    """Apply per-connection SQLite pragmas.

    ``foreign_keys`` is OFF by default in SQLite, which would silently ignore
    every ``ondelete="CASCADE"`` in the models. WAL mode lets reads proceed
    during writes, which matters once APScheduler jobs run alongside requests.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped SQLAlchemy session.

    Commits nothing implicitly -- routes commit explicitly. Always rolls back
    and closes, even when the handler raises.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional session for code outside the request cycle.

    Use in scheduler jobs, CLI scripts and agent tools, where ``Depends`` is
    unavailable. Commits on success, rolls back on error.

    Example:
        with session_scope() as db:
            db.add(SubUnitProgress(...))
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Bring the database up to the latest migration.

    Alembic owns the schema, not ``create_all``. That distinction matters:
    ``create_all`` silently skips tables that already exist and cannot add a
    column to one, so a new column appears to deploy fine and then fails at
    runtime with "no such column". Adding the question bank hit exactly that.

    Falls back to ``create_all`` only when Alembic is unavailable, and says so
    loudly -- that path cannot apply column changes.
    """
    try:
        from alembic import command
        from alembic.config import Config

        alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
        if not alembic_ini.is_file():
            raise FileNotFoundError(alembic_ini)

        config = Config(str(alembic_ini))
        config.set_main_option("script_location", str(alembic_ini.parent / "migrations"))
        config.set_main_option("sqlalchemy.url", settings.SQLALCHEMY_DATABASE_URL)
        command.upgrade(config, "head")
        logger.info(
            "SQLite migrated to head at %s (%d tables)",
            settings.SQLITE_PATH,
            len(Base.metadata.tables),
        )
        return
    except Exception as exc:
        logger.error(
            "Alembic migration failed (%s). Falling back to create_all, which "
            "creates missing tables but CANNOT add columns to existing ones.",
            exc,
        )

    Base.metadata.create_all(bind=engine)
    logger.warning(
        "SQLite ready at %s via create_all (%d tables) - schema may be stale.",
        settings.SQLITE_PATH,
        len(Base.metadata.tables),
    )


def drop_db() -> None:
    """Drop every table. Development convenience -- never call in production."""
    if settings.is_production:
        raise RuntimeError("drop_db() refused: ENVIRONMENT is production.")
    Base.metadata.drop_all(bind=engine)
    logger.warning("All SQLite tables dropped.")


def check_sql_connection() -> bool:
    """Return True if a trivial query against SQLite succeeds."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - infrastructure failure
        logger.error("SQLite health check failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# MongoDB (async, optional)
# --------------------------------------------------------------------------- #

# Collection names, centralised so no module hard-codes a string.
COLLECTION_CURRICULUM_CHUNKS = "curriculum_chunks"
COLLECTION_PARSED_DOCUMENTS = "parsed_documents"
COLLECTION_AGENT_TRACES = "agent_traces"
COLLECTION_GENERATED_CACHE = "generated_cache"

_mongo_client: Any | None = None
_mongo_db: Any | None = None


async def connect_to_mongo() -> Any | None:
    """Open the MongoDB connection and verify it with a ping.

    Returns:
        The database handle, or None if Mongo is unconfigured/unreachable.
    """
    global _mongo_client, _mongo_db

    if not settings.mongodb_configured:
        logger.warning("MONGODB_URI not set - document storage disabled.")
        return None

    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        _mongo_client = AsyncIOMotorClient(
            settings.MONGODB_URI,
            serverSelectionTimeoutMS=settings.MONGODB_TIMEOUT_MS,
            uuidRepresentation="standard",
        )
        await _mongo_client.admin.command("ping")
        _mongo_db = _mongo_client[settings.MONGODB_DB_NAME]
        await _ensure_mongo_indexes(_mongo_db)
        logger.info("MongoDB connected: db=%s", settings.MONGODB_DB_NAME)
        return _mongo_db
    except Exception as exc:
        logger.error("MongoDB connection failed (continuing without it): %s", exc)
        _mongo_client = None
        _mongo_db = None
        return None


async def _ensure_mongo_indexes(database: Any) -> None:
    """Create the indexes the RAG pipeline queries against."""
    chunks = database[COLLECTION_CURRICULUM_CHUNKS]
    await chunks.create_index("chunk_id", unique=True)
    await chunks.create_index([("unit_id", 1), ("sub_unit_id", 1)])
    await chunks.create_index("source_document")

    documents = database[COLLECTION_PARSED_DOCUMENTS]
    await documents.create_index("filename")
    await documents.create_index("content_hash", unique=True)

    traces = database[COLLECTION_AGENT_TRACES]
    await traces.create_index([("agent_name", 1), ("created_at", -1)])


async def close_mongo_connection() -> None:
    """Close the MongoDB connection on shutdown."""
    global _mongo_client, _mongo_db
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None
        _mongo_db = None
        logger.info("MongoDB connection closed.")


def get_mongo_db() -> Any | None:
    """Return the MongoDB handle, or None when unconfigured.

    Callers must handle None -- Mongo-backed features are optional by design.
    """
    return _mongo_db


def get_mongo_collection(name: str) -> Any | None:
    """Return one MongoDB collection, or None when Mongo is unavailable."""
    database = get_mongo_db()
    return database[name] if database is not None else None


async def check_mongo_connection() -> bool:
    """Return True if MongoDB answers a ping."""
    if _mongo_client is None:
        return False
    try:
        await _mongo_client.admin.command("ping")
        return True
    except Exception as exc:  # pragma: no cover - infrastructure failure
        logger.error("MongoDB health check failed: %s", exc)
        return False


__all__ = [
    "engine",
    "SessionLocal",
    "get_db",
    "session_scope",
    "init_db",
    "drop_db",
    "check_sql_connection",
    "connect_to_mongo",
    "close_mongo_connection",
    "get_mongo_db",
    "get_mongo_collection",
    "check_mongo_connection",
    "COLLECTION_CURRICULUM_CHUNKS",
    "COLLECTION_PARSED_DOCUMENTS",
    "COLLECTION_AGENT_TRACES",
    "COLLECTION_GENERATED_CACHE",
]
