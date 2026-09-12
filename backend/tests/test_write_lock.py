"""The write lock must never be held across a model call.

The bug this guards against, as a parent hit it: upload a curriculum, then try
to add a child, and get ``database is locked``.

SQLite allows one writer. The lock is taken at the first write of a transaction
and released only on commit. ``ensure_vocabulary`` wrote the derived skill tags
and flushed -- taking the lock -- and the caller then spent the better part of a
minute generating and verifying questions before committing. Every other write
in the app queued behind it and gave up after ``busy_timeout``.

These tests use a **file** database with two independent connections, because
the failure does not exist in the in-memory single-connection setup the rest of
the suite uses -- which is exactly why it reached a user. Generation is stubbed
with a callback that tries to write from the second connection at the moment
the real code would be talking to the model.

Run:
    cd backend
    pytest tests/test_write_lock.py -v
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, CurriculumSubUnit, CurriculumUnit
from services.skill_taxonomy import ensure_vocabulary

# Short on purpose: the real setting is 30s, and a test that waited that long to
# prove a lock is held would add half a minute to every run.
BUSY_TIMEOUT_MS = 300


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "lock.db"


def make_engine(path: Path):  # noqa: ANN201 - SQLAlchemy Engine
    """An engine configured the way the app configures its own."""
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    return engine


@pytest.fixture()
def db(db_path: Path) -> Iterator[Session]:
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def sub_unit(db: Session) -> CurriculumSubUnit:
    unit = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    sub = CurriculumSubUnit(
        unit=unit,
        sub_unit_number="1.1",
        sequence=0,
        title="Find GCF and LCM",
        description="Find GCF and LCM using a variety of strategies.",
    )
    db.add_all([unit, sub])
    db.commit()
    return sub


def other_writer(path: Path) -> Callable[[], bool]:
    """A second connection that tries to write, the way another request would.

    Returns a callable answering "could somebody else write just now?".
    """

    def attempt() -> bool:
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.execute(
                "INSERT INTO users (id, email, hashed_password, full_name, role,"
                " timezone, is_active, is_verified, email_opt_in, created_at, updated_at)"
                " VALUES (?, ?, 'x', 'Parent', 'parent', 'UTC', 1, 0, 1,"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (f"u{attempt.counter}", f"p{attempt.counter}@example.com"),
            )
            connection.commit()
            attempt.counter += 1
            return True
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc), exc
            return False
        finally:
            connection.close()

    attempt.counter = 0  # type: ignore[attr-defined]
    return attempt


class SpyLLM:
    """Stands in for the model, and checks the lock while it "thinks"."""

    def __init__(self, content: str, during: Callable[[], bool]) -> None:
        self.content = content
        self.during = during
        self.could_others_write: list[bool] = []

    def invoke(self, messages: list[dict[str, str]]) -> Any:  # noqa: ANN401
        # The real client is on the network here for seconds at a time. This is
        # the exact moment the app must not be holding the write lock.
        self.could_others_write.append(self.during())

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = text

        return _Response(self.content)


# --------------------------------------------------------------------------- #
# The harness itself must be able to detect a held lock
# --------------------------------------------------------------------------- #


def test_a_held_write_lock_is_actually_detected(db: Session, db_path: Path) -> None:
    """Without this, every other test here could pass by not testing anything.

    An uncommitted write holds the lock, so the second connection must fail.
    """
    attempt = other_writer(db_path)
    assert attempt() is True, "the writer should succeed when nothing holds the lock"

    db.execute(
        text(
            "INSERT INTO curriculum_units (id, unit_number, title, subject,"
            " grade_level, total_sub_units, is_active, created_at, updated_at)"
            " VALUES ('held', 99, 'Held', 'math', 6, 0, 1,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    )
    db.flush()

    assert attempt() is False, "a flushed, uncommitted write should hold the lock"
    db.rollback()
    assert attempt() is True, "rolling back should release it"


# --------------------------------------------------------------------------- #
# The fix
# --------------------------------------------------------------------------- #


def test_the_lock_is_free_while_the_caller_generates(
    db: Session, db_path: Path, sub_unit: CurriculumSubUnit
) -> None:
    """The actual regression, in the order it actually happened.

    ``ensure_vocabulary`` calls the model *first* and writes *after*, so its own
    model call was never the problem -- checking that window would pass either
    way. The damage was done afterwards: the write left the lock held, and the
    caller then spent the better part of a minute generating and verifying
    questions before committing.

    So this reproduces the caller: derive the vocabulary, then do what
    ``generate()`` does next, and check that somebody else can still write
    throughout.
    """
    attempt = other_writer(db_path)
    llm = SpyLLM('["find_gcf", "find_lcm", "use_prime_factorization"]', lambda: True)

    ensure_vocabulary(db, sub_unit, llm)

    # Where the caller now spends ~40s on retrieval, generation and
    # verification. Nothing it does here touches the database.
    assert attempt() is True, "the write lock was still held after the vocabulary was stored"
    assert attempt() is True, "and it stayed held for a second model call"


def test_the_vocabulary_survives_the_callers_rollback(
    db: Session, db_path: Path, sub_unit: CurriculumSubUnit
) -> None:
    """It commits, so a failed generation does not throw it away.

    Re-deriving costs another model call, and the vocabulary is valid whether
    or not the batch that prompted it worked.
    """
    llm = SpyLLM('["find_gcf", "find_lcm", "use_prime_factorization"]', lambda: True)
    ensure_vocabulary(db, sub_unit, llm)

    db.rollback()
    db.expire_all()

    stored = db.get(CurriculumSubUnit, sub_unit.id)
    assert stored is not None
    assert stored.skill_tags == ["find_gcf", "find_lcm", "use_prime_factorization"]


def test_others_can_write_once_the_vocabulary_is_stored(
    db: Session, db_path: Path, sub_unit: CurriculumSubUnit
) -> None:
    """And the lock is not left held afterwards either."""
    llm = SpyLLM('["find_gcf", "find_lcm", "use_prime_factorization"]', lambda: True)
    ensure_vocabulary(db, sub_unit, llm)

    assert other_writer(db_path)() is True
