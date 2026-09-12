"""Removing a curriculum.

This is the most destructive action in the app, and the damage is not
guessable from the outside: curriculum is the root of everything, so units own
sub-units, which own the quizzes taken against them, the progress earned on
them, and the pre-generated bank. A parent clicking a cross next to a PDF is
also deleting their child's history.

So the properties worth pinning are about honesty and blast radius:

* the preview counts what would actually go, and touches nothing;
* deletion takes exactly that and no more;
* points, levels and characters survive, because they belong to the child
  rather than to the curriculum;
* one family's deletion never reaches another's.

Run:
    cd backend
    pytest tests/test_curriculum_delete.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    CurriculumUpload,
    DifficultyLevel,
    GamificationProfile,
    QuestionBankItem,
    Quiz,
    QuizStatus,
    Student,
    SubUnitProgress,
    UploadStatus,
    User,
)
from services.curriculum_delete import delete_curriculum, preview


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def build_family(db: Session, email: str, units: int = 2) -> dict[str, Any]:
    """A parent with a curriculum, a child, and a history of using it."""
    user = User(email=email, hashed_password="x", full_name="Parent")
    db.add(user)
    db.flush()
    student = Student(parent=user, first_name="Kid", grade_level=6)
    db.add(student)
    db.flush()

    subs: list[CurriculumSubUnit] = []
    for number in range(1, units + 1):
        unit = CurriculumUnit(
            user_id=user.id,
            unit_number=number,
            title=f"Unit {number}",
            subject="math",
            grade_level=6,
        )
        db.add(unit)
        db.flush()
        sub = CurriculumSubUnit(
            unit=unit, sub_unit_number=f"{number}.1", sequence=0, title=f"Topic {number}.1"
        )
        db.add(sub)
        db.flush()
        subs.append(sub)

        db.add(
            QuestionBankItem(
                sub_unit_id=sub.id,
                difficulty_level=DifficultyLevel.BEGINNER,
                question_text=f"Q for {number}.1",
                correct_answer="A",
                explanation="Because.",
                content_hash=f"h{number}",
            )
        )
        db.add(
            SubUnitProgress(
                student_id=student.id,
                sub_unit_id=sub.id,
                completion_percentage=33,
                total_attempts=1,
            )
        )
        db.add(
            Quiz(
                student_id=student.id,
                sub_unit_id=sub.id,
                difficulty_level=DifficultyLevel.BEGINNER,
                status=QuizStatus.COMPLETED,
                total_questions=10,
                passing_threshold=70.0,
            )
        )

    db.add(
        CurriculumUpload(
            user_id=user.id,
            filename="theirs.pdf",
            subject="math",
            grade_level=6,
            status=UploadStatus.COMPLETED,
        )
    )
    db.add(GamificationProfile(student_id=student.id, total_points=2000, level=5))
    db.commit()
    return {"user": user, "student": student, "subs": subs}


# --------------------------------------------------------------------------- #
# The preview
# --------------------------------------------------------------------------- #


def test_the_preview_counts_what_would_go(db: Session) -> None:
    family = build_family(db, "a@example.com", units=2)

    report = preview(db, family["user"].id)
    assert report.units == 2
    assert report.sub_units == 2
    assert report.quizzes == 2
    assert report.progress_rows == 2
    assert report.bank_questions == 2
    assert report.uploads == 1


def test_the_preview_deletes_nothing(db: Session) -> None:
    """It exists so a parent can see the cost. It must not be the cost."""
    family = build_family(db, "b@example.com", units=2)

    preview(db, family["user"].id)

    assert db.query(CurriculumUnit).count() == 2
    assert db.query(Quiz).count() == 2
    assert db.query(SubUnitProgress).count() == 2


def test_an_owner_with_no_curriculum_previews_nothing(db: Session) -> None:
    user = User(email="empty@example.com", hashed_password="x", full_name="Parent")
    db.add(user)
    db.commit()

    report = preview(db, user.id)
    assert report.is_empty
    assert report.units == 0


# --------------------------------------------------------------------------- #
# The deletion
# --------------------------------------------------------------------------- #


def test_deleting_takes_the_whole_tree(db: Session) -> None:
    """Not guessable from a cross next to a PDF, which is why it is spelled out."""
    family = build_family(db, "c@example.com", units=2)

    report = delete_curriculum(db, family["user"].id, drop_vectors=False)
    db.commit()

    assert report.units == 2
    assert db.query(CurriculumUnit).count() == 0
    assert db.query(CurriculumSubUnit).count() == 0
    assert db.query(Quiz).count() == 0
    assert db.query(SubUnitProgress).count() == 0
    assert db.query(QuestionBankItem).count() == 0
    assert db.query(CurriculumUpload).count() == 0


def test_the_child_keeps_their_points_and_level(db: Session) -> None:
    """They belong to the child, not the curriculum.

    A child who saved 2,000 points and bought a dragon must not lose it
    because a parent replaced a PDF.
    """
    family = build_family(db, "d@example.com", units=2)

    delete_curriculum(db, family["user"].id, drop_vectors=False)
    db.commit()

    profile = db.query(GamificationProfile).filter_by(student_id=family["student"].id).one()
    assert profile.total_points == 2000
    assert profile.level == 5
    assert db.query(Student).count() == 1, "the child themselves must survive too"


def test_deleting_one_family_leaves_another_untouched(db: Session) -> None:
    mine = build_family(db, "mine@example.com", units=2)
    theirs = build_family(db, "theirs@example.com", units=3)

    delete_curriculum(db, mine["user"].id, drop_vectors=False)
    db.commit()

    survivors = db.query(CurriculumUnit).all()
    assert len(survivors) == 3
    assert {unit.user_id for unit in survivors} == {theirs["user"].id}
    assert db.query(SubUnitProgress).count() == 3


def test_deleting_nothing_is_not_an_error(db: Session) -> None:
    """A double-click on the cross must not raise on the second one."""
    family = build_family(db, "e@example.com", units=1)

    delete_curriculum(db, family["user"].id, drop_vectors=False)
    db.commit()
    again = delete_curriculum(db, family["user"].id, drop_vectors=False)
    db.commit()

    assert again.is_empty


def test_the_report_matches_the_preview(db: Session) -> None:
    """What a parent was shown is what actually happened."""
    family = build_family(db, "f@example.com", units=3)

    promised = preview(db, family["user"].id)
    delivered = delete_curriculum(db, family["user"].id, drop_vectors=False)
    db.commit()

    # Every count must match. `vectors_dropped` is excluded because the preview
    # never touches Pinecone and so can never report it.
    counted = lambda report: {  # noqa: E731
        key: value for key, value in vars(report).items() if key != "vectors_dropped"
    }
    assert counted(delivered) == counted(promised)
