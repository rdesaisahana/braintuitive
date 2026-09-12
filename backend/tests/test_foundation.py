"""Phase 1 foundation tests.

Verifies the pieces the rest of the project is built on: the schema creates,
cascades and constraints behave, and the 0/33/67/100 progression rules match
the product spec. Runs entirely against an in-memory SQLite database, so no
external services are required.

Run:
    cd backend
    pytest tests/test_foundation.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    AchievementBadge,
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    GamificationProfile,
    Notification,
    NotificationType,
    ProgressStatus,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizResponse,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
    UserRole,
)
from db.models import (
    Session as UserSession,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db() -> Session:
    """An isolated in-memory database with foreign keys enforced."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


@pytest.fixture()
def student(db: Session) -> Student:
    """A parent account with one child profile."""
    user = User(
        email="parent@example.com",
        hashed_password="not-a-real-hash",
        full_name="Test Parent",
        role=UserRole.PARENT,
    )
    child = Student(parent=user, first_name="Aanya", last_name="R", grade_level=6)
    db.add(user)
    db.add(child)
    db.commit()
    return child


@pytest.fixture()
def sub_unit(db: Session) -> CurriculumSubUnit:
    """Unit 1 with a single sub-unit 1.1."""
    unit = CurriculumUnit(
        unit_number=1,
        title="Integers and Rational Numbers",
        subject="math",
        grade_level=6,
        source_document="06 Pre-Algebra H&A.pdf",
    )
    sub = CurriculumSubUnit(
        unit=unit,
        sub_unit_number="1.1",
        sequence=0,
        title="Adding Integers",
        learning_objectives=["Add integers with like signs", "Add integers with unlike signs"],
    )
    db.add(unit)
    db.add(sub)
    db.commit()
    return sub


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_schema_table_count() -> None:
    """13 core tables plus question_bank, added for pre-generation."""
    assert len(Base.metadata.tables) == 15, sorted(Base.metadata.tables)


def test_expected_table_names_present() -> None:
    expected = {
        "users",
        "students",
        "curriculum_units",
        "curriculum_sub_units",
        "quizzes",
        "quiz_questions",
        "quiz_responses",
        "quiz_attempts",
        "sub_unit_progress",
        "gamification_profiles",
        "achievement_badges",
        "notifications",
        "sessions",
        "question_bank",
        "curriculum_uploads",
    }
    assert set(Base.metadata.tables) == expected


def test_uuid_primary_keys_are_generated(db: Session, student: Student) -> None:
    assert len(student.id) == 36
    assert student.user_id is not None


def test_duplicate_email_rejected(db: Session, student: Student) -> None:
    db.add(User(email="parent@example.com", hashed_password="x", full_name="Impostor"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_cascade_delete_removes_students(db: Session, student: Student) -> None:
    parent = student.parent
    db.delete(parent)
    db.commit()
    assert db.query(Student).count() == 0


def test_enum_stores_value_not_member_name(db: Session, student: Student) -> None:
    """Raw column should read 'parent', not 'PARENT'."""
    from sqlalchemy import text

    stored = db.execute(text("SELECT role FROM users LIMIT 1")).scalar_one()
    assert stored == "parent"


# --------------------------------------------------------------------------- #
# Difficulty thresholds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("level", "threshold"),
    [
        (DifficultyLevel.BEGINNER, 70.0),
        (DifficultyLevel.INTERMEDIATE, 80.0),
        (DifficultyLevel.PROFICIENT, 90.0),
    ],
)
def test_difficulty_thresholds(level: DifficultyLevel, threshold: float) -> None:
    assert level.threshold == threshold


# --------------------------------------------------------------------------- #
# Progression: 0 / 33 / 67 / 100
# --------------------------------------------------------------------------- #


def test_progress_starts_at_zero(db: Session, student: Student, sub_unit) -> None:
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id)
    db.add(progress)
    db.commit()

    assert progress.completion_percentage == 0
    assert progress.status is ProgressStatus.NOT_STARTED
    assert progress.next_difficulty is DifficultyLevel.BEGINNER


def test_progress_steps_through_thirds(db: Session, student: Student, sub_unit) -> None:
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id)
    db.add(progress)

    assert progress.mark_level_complete(DifficultyLevel.BEGINNER, 75.0) == 33
    assert progress.status is ProgressStatus.IN_PROGRESS
    assert progress.next_difficulty is DifficultyLevel.INTERMEDIATE

    assert progress.mark_level_complete(DifficultyLevel.INTERMEDIATE, 85.0) == 67
    assert progress.next_difficulty is DifficultyLevel.PROFICIENT

    assert progress.mark_level_complete(DifficultyLevel.PROFICIENT, 95.0) == 100
    assert progress.status is ProgressStatus.COMPLETED
    assert progress.next_difficulty is None
    assert progress.completed_at is not None
    db.commit()


def test_failing_score_does_not_advance(db: Session, student: Student, sub_unit) -> None:
    """65% on beginner is below the 70% threshold - still 0%."""
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id)
    progress.total_attempts = 1

    assert progress.record_score(DifficultyLevel.BEGINNER, 65.0) == 0
    assert progress.beginner_completed is False
    assert progress.beginner_best_score == 65.0
    assert progress.status is ProgressStatus.IN_PROGRESS


def test_best_score_is_kept_across_retries(db: Session, student: Student, sub_unit) -> None:
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id)
    progress.record_score(DifficultyLevel.BEGINNER, 80.0)
    progress.record_score(DifficultyLevel.BEGINNER, 55.0)  # worse retry
    assert progress.beginner_best_score == 80.0
    assert progress.beginner_completed is True


def test_celebration_fires_once_and_only_at_100(db: Session, student: Student, sub_unit) -> None:
    progress = SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id)

    progress.mark_level_complete(DifficultyLevel.BEGINNER, 90.0)
    assert progress.should_celebrate is False, "must not celebrate at 33%"

    progress.mark_level_complete(DifficultyLevel.INTERMEDIATE, 90.0)
    assert progress.should_celebrate is False, "must not celebrate at 67%"

    progress.mark_level_complete(DifficultyLevel.PROFICIENT, 95.0)
    assert progress.should_celebrate is True, "should celebrate at 100%"

    progress.celebration_shown = True
    assert progress.should_celebrate is False, "must not celebrate twice"


def test_percentage_constraint_rejects_off_step_values(
    db: Session, student: Student, sub_unit
) -> None:
    """The CHECK constraint should only permit 0/33/67/100."""
    db.add(
        SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id, completion_percentage=50)
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_one_progress_row_per_student_sub_unit(db: Session, student: Student, sub_unit) -> None:
    db.add(SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id))
    db.commit()
    db.add(SubUnitProgress(student_id=student.id, sub_unit_id=sub_unit.id))
    with pytest.raises(IntegrityError):
        db.commit()


# --------------------------------------------------------------------------- #
# Quizzes
# --------------------------------------------------------------------------- #


def test_quiz_score_computation(db: Session, student: Student, sub_unit) -> None:
    quiz = Quiz(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        difficulty_level=DifficultyLevel.INTERMEDIATE,
        status=QuizStatus.COMPLETED,
        total_questions=10,
        correct_count=8,
        passing_threshold=DifficultyLevel.INTERMEDIATE.threshold,
    )
    assert quiz.compute_score() == 80.0
    assert quiz.is_passed is True

    quiz.correct_count = 7
    assert quiz.compute_score() == 70.0
    assert quiz.is_passed is False, "70% does not clear the 80% intermediate bar"


def test_quiz_question_and_response_round_trip(db: Session, student: Student, sub_unit) -> None:
    quiz = Quiz(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        difficulty_level=DifficultyLevel.BEGINNER,
        passing_threshold=70.0,
    )
    question = QuizQuestion(
        quiz=quiz,
        question_number=1,
        question_text="What is -3 + 8?",
        options=[
            {"key": "A", "text": "5"},
            {"key": "B", "text": "-5"},
            {"key": "C", "text": "11"},
            {"key": "D", "text": "-11"},
        ],
        correct_answer="A",
        explanation="Start at -3 and move 8 to the right on the number line: 5.",
        distractor_rationales={"B": "You kept the sign of the larger absolute value."},
        difficulty_level=DifficultyLevel.BEGINNER,
        skill_tag="add_integers",
        source_chunk_ids=["chunk-1", "chunk-2"],
    )
    db.add(quiz)
    db.add(question)
    db.commit()

    response = QuizResponse(
        quiz_id=quiz.id,
        question_id=question.id,
        student_id=student.id,
        selected_answer="A",
        is_correct=True,
        time_spent_seconds=12,
        feedback_shown=True,
    )
    db.add(response)
    db.commit()

    saved = db.query(QuizQuestion).one()
    assert saved.options[0]["text"] == "5"  # JSON column round-trips
    assert saved.source_chunk_ids == ["chunk-1", "chunk-2"]
    assert len(quiz.questions) == 1
    assert db.query(QuizResponse).one().is_correct is True


def test_duplicate_question_number_rejected(db: Session, student: Student, sub_unit) -> None:
    quiz = Quiz(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        difficulty_level=DifficultyLevel.BEGINNER,
        passing_threshold=70.0,
    )
    db.add(quiz)
    db.commit()

    for _ in range(2):
        db.add(
            QuizQuestion(
                quiz_id=quiz.id,
                question_number=1,
                question_text="dup",
                correct_answer="A",
                explanation="...",
                difficulty_level=DifficultyLevel.BEGINNER,
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


def test_quiz_attempt_history(db: Session, student: Student, sub_unit) -> None:
    quiz = Quiz(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        difficulty_level=DifficultyLevel.PROFICIENT,
        passing_threshold=90.0,
        total_questions=10,
        correct_count=9,
    )
    db.add(quiz)
    db.commit()

    attempt = QuizAttempt(
        student_id=student.id,
        sub_unit_id=sub_unit.id,
        quiz_id=quiz.id,
        difficulty_level=DifficultyLevel.PROFICIENT,
        attempt_number=2,
        total_questions=10,
        correct_count=9,
        score_percentage=90.0,
        is_passed=True,
        skill_breakdown={"add_integers": 1.0, "subtract_integers": 0.8},
    )
    db.add(attempt)
    db.commit()

    assert db.query(QuizAttempt).one().skill_breakdown["subtract_integers"] == 0.8
    assert student.attempts[0].attempt_number == 2


# --------------------------------------------------------------------------- #
# Gamification
# --------------------------------------------------------------------------- #


def test_points_and_level_up(db: Session, student: Student) -> None:
    profile = GamificationProfile(student_id=student.id)
    db.add(profile)

    assert profile.award_points(50) is False
    assert profile.level == 1

    assert profile.award_points(60) is True, "110 points clears the 100-point bar"
    assert profile.level == 2
    assert profile.total_points == 110
    db.commit()


def test_badge_unique_per_student(db: Session, student: Student) -> None:
    for _ in range(2):
        db.add(
            AchievementBadge(
                student_id=student.id,
                badge_key="first_100_percent",
                badge_name="Sub-Unit Master",
                tier="gold",
                points_awarded=50,
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


def test_gamification_profile_is_one_to_one(db: Session, student: Student) -> None:
    db.add(GamificationProfile(student_id=student.id))
    db.commit()
    db.refresh(student)
    assert student.gamification_profile is not None
    assert student.gamification_profile.avatar_key == "sunny"


# --------------------------------------------------------------------------- #
# Notifications & sessions
# --------------------------------------------------------------------------- #


def test_notification_defaults(db: Session, student: Student) -> None:
    note = Notification(
        user_id=student.user_id,
        student_id=student.id,
        type=NotificationType.UNIT_COMPLETE,
        subject="Aanya finished Unit 1",
        body="All sub-units are at 100%.",
        payload={"unit_number": 1},
    )
    db.add(note)
    db.commit()

    assert note.is_sent is False
    assert note.payload["unit_number"] == 1


def test_session_activity_window(db: Session, student: Student) -> None:
    live = UserSession(
        user_id=student.user_id,
        refresh_token_hash="hash-a",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    stale = UserSession(
        user_id=student.user_id,
        refresh_token_hash="hash-b",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    revoked = UserSession(
        user_id=student.user_id,
        refresh_token_hash="hash-c",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        revoked_at=datetime.now(UTC),
    )
    db.add_all([live, stale, revoked])
    db.commit()

    assert live.is_active is True
    assert stale.is_active is False
    assert revoked.is_active is False
