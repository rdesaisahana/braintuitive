"""Question bank and practice-mode tests.

The bank is what removes ~2 minutes of generation from the request path. These
tests cover the three properties that matter:

* a quiz assembled from the bank is a real quiz, indistinguishable to the
  student from a generated one;
* a retry hands the student questions they have not seen before;
* practising after mastery cannot damage a score.

Generation is stubbed, so nothing here costs an API call.

Run:
    cd backend
    pytest tests/test_question_bank.py -v
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    Base,
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    QuestionBankItem,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
)
from services.question_bank import (
    content_hash,
    draw_questions,
    mark_served,
    retire,
    seen_bank_ids,
    slot_status,
    total_servable,
)
from services.quiz_service import create_quiz, record_attempt
from services.scheduler_jobs import student_frontier

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


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


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """Two units, one student, and a stocked bank for sub-unit 1.2 beginner."""
    unit1 = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    unit2 = CurriculumUnit(unit_number=2, title="Expressions", subject="math", grade_level=6)
    sub11 = CurriculumSubUnit(
        unit=unit1,
        sub_unit_number="1.1",
        sequence=0,
        title="Absolute value",
        is_indexed=True,
        skill_tags=["find_absolute_value", "compare_absolute_values"],
    )
    sub12 = CurriculumSubUnit(
        unit=unit1,
        sub_unit_number="1.2",
        sequence=1,
        title="Add integers",
        is_indexed=True,
        skill_tags=["add_integers", "subtract_integers"],
    )
    sub21 = CurriculumSubUnit(
        unit=unit2,
        sub_unit_number="2.1",
        sequence=0,
        title="Expressions",
        is_indexed=True,
        skill_tags=["apply_properties", "simplify_expressions"],
    )
    user = User(email="p@example.com", hashed_password="x", full_name="Parent")
    student = Student(parent=user, first_name="Aanya", grade_level=6)
    db.add_all([unit1, unit2, sub11, sub12, sub21, user, student])
    db.commit()
    return {
        "unit1": unit1,
        "unit2": unit2,
        "sub11": sub11,
        "sub12": sub12,
        "sub21": sub21,
        "student": student,
    }


def stock_bank(
    db: Session,
    sub_unit: CurriculumSubUnit,
    difficulty: DifficultyLevel = DifficultyLevel.BEGINNER,
    count: int = 30,
    status: str = "verified",
    offset: int = 0,
) -> list[QuestionBankItem]:
    """Insert ``count`` distinct bank questions into one slot.

    ``offset`` shifts the generated text so two calls on the same slot do not
    collide on the content-hash uniqueness constraint.
    """
    items = []
    for raw in range(count):
        index = raw + offset
        text = f"What is {index} + {index + 1}?"
        item = QuestionBankItem(
            sub_unit_id=sub_unit.id,
            difficulty_level=difficulty,
            question_text=text,
            options=[{"key": k, "text": f"{index}{k}"} for k in "ABCD"],
            correct_answer="ABCD"[index % 4],
            explanation="Add the two numbers together to get the total.",
            distractor_rationales={k: f"mistake {k}" for k in "ABCD" if k != "ABCD"[index % 4]},
            skill_tag="add_integers",
            content_hash=content_hash(text),
            verification_status=status,
            is_active=status != "disputed",
        )
        db.add(item)
        items.append(item)
    db.commit()
    return items


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_the_bank_has_its_own_table() -> None:
    """Asserts the table exists rather than counting them.

    The count assertion this replaces broke on every unrelated table added --
    it failed twice for tables that had nothing to do with the bank, which is
    noise rather than signal. `test_schema_table_count` in test_foundation.py
    is the one place that still pins the total.
    """
    assert "question_bank" in Base.metadata.tables


def test_bank_dedupes_within_a_slot(db: Session, world: dict[str, Any]) -> None:
    """The same question must not appear twice in one slot."""
    from sqlalchemy.exc import IntegrityError

    for _ in range(2):
        db.add(
            QuestionBankItem(
                sub_unit_id=world["sub12"].id,
                difficulty_level=DifficultyLevel.BEGINNER,
                question_text="What is 2 + 2?",
                correct_answer="A",
                explanation="Add them together to reach four.",
                content_hash=content_hash("What is 2 + 2?"),
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_question_allowed_in_different_difficulty(db: Session, world: dict[str, Any]) -> None:
    for difficulty in (DifficultyLevel.BEGINNER, DifficultyLevel.INTERMEDIATE):
        db.add(
            QuestionBankItem(
                sub_unit_id=world["sub12"].id,
                difficulty_level=difficulty,
                question_text="What is 2 + 2?",
                correct_answer="A",
                explanation="Add them together to reach four.",
                content_hash=content_hash("What is 2 + 2?"),
            )
        )
    db.commit()
    assert db.query(QuestionBankItem).count() == 2


def test_content_hash_ignores_whitespace_and_case() -> None:
    assert content_hash("What is  2 + 2?") == content_hash("what is 2 + 2?")
    assert content_hash("What is 2 + 2?") != content_hash("What is 3 + 3?")


# --------------------------------------------------------------------------- #
# Servability
# --------------------------------------------------------------------------- #


def test_disputed_items_are_never_served(db: Session, world: dict[str, Any]) -> None:
    """A question the verifier rejected must not reach a student."""
    stock_bank(db, world["sub12"], count=5, status="disputed")
    items, _ = draw_questions(
        db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER, count=5
    )
    assert items == []
    assert total_servable(db) == 0


def test_retire_is_soft_and_stops_serving(db: Session, world: dict[str, Any]) -> None:
    """Answer history points at bank rows, so retirement must not delete."""
    items = stock_bank(db, world["sub12"], count=3)
    retire(db, items[0].id, "bad maths spotted in review")
    db.commit()

    assert db.query(QuestionBankItem).count() == 3, "the row must survive"
    assert items[0].is_active is False
    assert items[0].review_notes

    drawn, _ = draw_questions(
        db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER, count=3
    )
    assert items[0].id not in [d.id for d in drawn]


def test_slot_status_counts_correctly(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=8)
    stock_bank(db, world["sub12"], count=2, status="disputed", offset=100)

    status = slot_status(db, world["sub12"], DifficultyLevel.BEGINNER, target=30)
    assert status.servable == 8
    assert status.shortfall == 22
    assert status.is_stocked is False


# --------------------------------------------------------------------------- #
# Drawing and freshness
# --------------------------------------------------------------------------- #


def test_draw_returns_requested_count(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    items, fresh = draw_questions(
        db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER, count=10
    )
    assert len(items) == 10
    assert fresh is True
    assert len({item.id for item in items}) == 10, "no duplicates within one quiz"


def test_draw_is_randomised(db: Session, world: dict[str, Any]) -> None:
    """Two students should not get the same ten questions in the same order."""
    stock_bank(db, world["sub12"], count=30)
    first, _ = draw_questions(
        db,
        world["student"].id,
        world["sub12"].id,
        DifficultyLevel.BEGINNER,
        count=10,
        rng=random.Random(1),
    )
    second, _ = draw_questions(
        db,
        world["student"].id,
        world["sub12"].id,
        DifficultyLevel.BEGINNER,
        count=10,
        rng=random.Random(2),
    )
    assert [i.id for i in first] != [i.id for i in second]


def test_empty_bank_draws_nothing(db: Session, world: dict[str, Any]) -> None:
    items, fresh = draw_questions(
        db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER, count=10
    )
    assert items == []
    assert fresh is True


def test_retry_gives_completely_fresh_questions(db: Session, world: dict[str, Any]) -> None:
    """The core of the feature: a second attempt must not repeat questions."""
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id

    first = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    first_ids = {q.bank_question_id for q in first.questions}

    second = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    second_ids = {q.bank_question_id for q in second.questions}

    assert len(first_ids) == 10
    assert first_ids & second_ids == set(), "the retry repeated a question"
    assert second.generation_metadata["all_questions_fresh"] is True


def test_three_fresh_attempts_then_repeats(db: Session, world: dict[str, Any]) -> None:
    """A 30-deep bank gives exactly three clean runs at ten questions."""
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id

    seen: set[str] = set()
    for attempt in range(3):
        quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
        db.commit()
        ids = {q.bank_question_id for q in quiz.questions}
        assert ids & seen == set(), f"attempt {attempt + 1} repeated a question"
        assert quiz.generation_metadata["all_questions_fresh"] is True
        seen |= ids

    assert len(seen) == 30

    fourth = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    assert fourth.generation_metadata["all_questions_fresh"] is False
    assert fourth.total_questions == 10, "a repeat still beats no quiz"


def test_seen_ids_are_scoped_to_student_and_slot(db: Session, world: dict[str, Any]) -> None:
    """One student's history must not restrict another's draw."""
    stock_bank(db, world["sub12"], count=30)
    other = Student(user_id=world["student"].user_id, first_name="Vihaan", grade_level=6)
    db.add(other)
    db.commit()

    create_quiz(db, world["student"].id, world["sub12"].id, "beginner")
    db.commit()

    assert (
        len(seen_bank_ids(db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER))
        == 10
    )
    assert seen_bank_ids(db, other.id, world["sub12"].id, DifficultyLevel.BEGINNER) == set()
    # A different difficulty is a different slot.
    assert (
        seen_bank_ids(db, world["student"].id, world["sub12"].id, DifficultyLevel.PROFICIENT)
        == set()
    )


def test_serving_updates_counters(db: Session, world: dict[str, Any]) -> None:
    items = stock_bank(db, world["sub12"], count=3)
    mark_served(db, items[:2])
    db.commit()

    assert items[0].times_served == 1
    assert items[0].last_served_at is not None
    assert items[2].times_served == 0


# --------------------------------------------------------------------------- #
# Quiz assembly from the bank
# --------------------------------------------------------------------------- #


def test_bank_quiz_carries_full_feedback(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    quiz = create_quiz(db, world["student"].id, world["sub12"].id, "beginner")
    db.commit()

    assert quiz.total_questions == 10
    assert quiz.passing_threshold == 70.0
    assert quiz.status is QuizStatus.PENDING
    assert quiz.generation_metadata["source"] == "question_bank"

    questions = db.query(QuizQuestion).filter_by(quiz_id=quiz.id).all()
    assert len(questions) == 10
    assert [q.question_number for q in questions] == list(range(1, 11))
    for question in questions:
        assert question.explanation
        assert question.distractor_rationales
        assert len(question.options) == 4
        assert question.bank_question_id is not None


def test_partial_bank_falls_back_rather_than_short_quiz(
    db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 6-question quiz would silently change the pass arithmetic."""
    stock_bank(db, world["sub12"], count=6)

    called: list[str] = []

    class FakeAgent:
        def generate(self, **kwargs: Any) -> Any:  # noqa: ANN401
            called.append("generate")
            raise RuntimeError("live generation reached")

    with pytest.raises(RuntimeError, match="live generation reached"):
        create_quiz(db, world["student"].id, world["sub12"].id, "beginner", agent=FakeAgent())

    assert called == ["generate"], "should fall back, not serve a short quiz"


def test_use_bank_false_forces_generation(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)

    class FakeAgent:
        def generate(self, **kwargs: Any) -> Any:  # noqa: ANN401
            raise RuntimeError("forced live generation")

    with pytest.raises(RuntimeError, match="forced live generation"):
        create_quiz(
            db,
            world["student"].id,
            world["sub12"].id,
            "beginner",
            agent=FakeAgent(),
            use_bank=False,
        )


def test_locked_unit_still_blocks_bank_quizzes(db: Session, world: dict[str, Any]) -> None:
    """The bank must not become a way around sequential progression."""
    from services.quiz_service import UnitLockedError

    stock_bank(db, world["sub21"], count=30)
    with pytest.raises(UnitLockedError):
        create_quiz(db, world["student"].id, world["sub21"].id, "beginner")


# --------------------------------------------------------------------------- #
# Practice mode
# --------------------------------------------------------------------------- #


def _complete_tier(
    db: Session, student_id: str, sub_unit_id: str, level: DifficultyLevel
) -> SubUnitProgress:
    progress = SubUnitProgress(student_id=student_id, sub_unit_id=sub_unit_id)
    progress.mark_level_complete(level, 95.0)
    db.add(progress)
    db.commit()
    return progress


def test_first_attempt_is_not_practice(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    quiz = create_quiz(db, world["student"].id, world["sub12"].id, "beginner")
    db.commit()
    assert quiz.is_practice is False


def test_retry_after_mastery_is_practice(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    _complete_tier(db, world["student"].id, world["sub12"].id, DifficultyLevel.BEGINNER)

    quiz = create_quiz(db, world["student"].id, world["sub12"].id, "beginner")
    db.commit()
    assert quiz.is_practice is True


def test_practice_cannot_lower_a_score(db: Session, world: dict[str, Any]) -> None:
    """The whole promise of practice mode."""
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id
    progress = _complete_tier(db, student_id, world["sub12"].id, DifficultyLevel.BEGINNER)
    assert progress.beginner_best_score == 95.0
    assert progress.completion_percentage == 33

    quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    record_attempt(db, quiz, correct_count=2)  # a dismal 20%
    db.commit()

    db.refresh(progress)
    assert progress.beginner_best_score == 95.0, "practice lowered a real score"
    assert progress.beginner_completed is True
    assert progress.completion_percentage == 33


def test_practice_attempt_is_still_recorded(db: Session, world: dict[str, Any]) -> None:
    """Practice should be invisible to progress but visible in history."""
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id
    _complete_tier(db, student_id, world["sub12"].id, DifficultyLevel.BEGINNER)

    quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    attempt = record_attempt(db, quiz, correct_count=4, duration_seconds=120)
    db.commit()

    assert attempt.is_practice is True
    assert attempt.score_percentage == 40.0
    assert db.query(QuizAttempt).count() == 1
    assert db.query(Quiz).filter_by(id=quiz.id).one().status is QuizStatus.COMPLETED


def test_real_attempt_moves_progress(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id

    quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    attempt = record_attempt(db, quiz, correct_count=9, duration_seconds=300)
    db.commit()

    assert attempt.is_practice is False
    assert attempt.is_passed is True
    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student_id, sub_unit_id=world["sub12"].id)
        .one()
    )
    assert progress.beginner_completed is True
    assert progress.beginner_best_score == 90.0
    assert progress.completion_percentage == 33
    assert progress.total_attempts == 1


def test_practice_after_100_percent_keeps_celebration_spent(
    db: Session, world: dict[str, Any]
) -> None:
    """A kid at 100% who practises must not re-trigger the celebration."""
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id

    progress = SubUnitProgress(student_id=student_id, sub_unit_id=world["sub12"].id)
    for level in DifficultyLevel:
        progress.mark_level_complete(level, 95.0)
    progress.celebration_shown = True
    db.add(progress)
    db.commit()
    assert progress.completion_percentage == 100

    quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
    db.commit()
    record_attempt(db, quiz, correct_count=1)
    db.commit()

    db.refresh(progress)
    assert progress.completion_percentage == 100
    assert progress.should_celebrate is False


def test_attempt_numbers_increment(db: Session, world: dict[str, Any]) -> None:
    stock_bank(db, world["sub12"], count=30)
    student_id = world["student"].id

    for expected in (1, 2, 3):
        quiz = create_quiz(db, student_id, world["sub12"].id, "beginner")
        db.commit()
        attempt = record_attempt(db, quiz, correct_count=5)
        db.commit()
        assert attempt.attempt_number == expected


# --------------------------------------------------------------------------- #
# Scheduler frontier
# --------------------------------------------------------------------------- #


def test_frontier_defaults_to_unit_one(db: Session, world: dict[str, Any]) -> None:
    assert student_frontier(db) == 1


def test_frontier_follows_the_furthest_student(db: Session, world: dict[str, Any]) -> None:
    db.add(SubUnitProgress(student_id=world["student"].id, sub_unit_id=world["sub21"].id))
    db.commit()
    assert student_frontier(db) == 2


# --------------------------------------------------------------------------- #
# Write-lock behaviour during a fill
# --------------------------------------------------------------------------- #


class BatchSpyAgent:
    """Stub generator that inspects the database from a separate connection.

    Between batches it opens its own connection and counts committed bank rows.
    That is the whole point: if ``fill_slot`` held its write transaction open
    across generation, this second connection would see nothing (and a
    concurrent writer would fail with "database is locked").
    """

    def __init__(self, engine_url: str, per_batch: int = 5) -> None:
        self.engine_url = engine_url
        self.per_batch = per_batch
        self.visible_row_counts: list[int] = []
        self.calls = 0

    def generate(
        self, sub_unit: str, difficulty: Any, count: int, db: Session
    ) -> Any:  # noqa: ANN401
        from sqlalchemy import create_engine as _create_engine
        from sqlalchemy import text as _text

        from agents.quiz_generator import GeneratedQuestion, GeneratedQuiz

        # A genuinely independent connection, as a concurrent worker would use.
        probe = _create_engine(self.engine_url)
        with probe.connect() as connection:
            visible = connection.execute(_text("SELECT COUNT(*) FROM question_bank")).scalar_one()
        probe.dispose()
        self.visible_row_counts.append(int(visible))

        self.calls += 1
        offset = self.calls * 1000
        questions = [
            GeneratedQuestion(
                question_number=n + 1,
                question_text=f"Batch question {offset + n}?",
                options=[{"key": k, "text": f"{k}{offset + n}"} for k in "ABCD"],
                correct_answer="A",
                explanation="An explanation long enough to pass validation checks.",
                distractor_rationales={k: f"mistake {k}" for k in "BCD"},
                skill_tag="stub",
            )
            for n in range(min(count, self.per_batch))
        ]
        return GeneratedQuiz(
            sub_unit_id=sub_unit,
            sub_unit_number="1.2",
            difficulty=difficulty,
            questions=questions,
        )


def test_fill_commits_between_batches(tmp_path: Any) -> None:
    """Regression: a live 3-worker fill failed with "database is locked".

    fill_slot held one transaction for the whole slot, so the first INSERT took
    SQLite's single write lock and kept it through the next batch's model call
    -- minutes -- starving every other worker.
    """
    from sqlalchemy import create_engine as _create_engine

    from services.question_bank import fill_slot

    db_path = tmp_path / "bank.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = _create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    unit = CurriculumUnit(unit_number=1, title="U1", subject="math", grade_level=6)
    sub = CurriculumSubUnit(
        unit=unit, sub_unit_number="1.2", sequence=0, title="S", is_indexed=True
    )
    session.add_all([unit, sub])
    session.commit()

    agent = BatchSpyAgent(url, per_batch=5)
    fill_slot(session, sub_unit=sub, difficulty=DifficultyLevel.BEGINNER, target=15, agent=agent)
    session.commit()

    assert agent.calls >= 3, "expected several batches"
    # The first batch sees an empty table; every later batch must see the rows
    # the previous batch committed.
    assert agent.visible_row_counts[0] == 0
    assert agent.visible_row_counts[1] >= 5, (
        "batch 2 could not see batch 1's rows - the transaction was still open, "
        f"saw {agent.visible_row_counts}"
    )
    assert agent.visible_row_counts[-1] >= 10, agent.visible_row_counts

    session.close()
    engine.dispose()


def test_duplicate_race_does_not_discard_the_batch(db: Session, world: dict[str, Any]) -> None:
    """A SAVEPOINT keeps one duplicate from rolling back its whole batch."""
    from agents.quiz_generator import GeneratedQuestion
    from services.question_bank import _store

    def question(text: str) -> GeneratedQuestion:
        return GeneratedQuestion(
            question_number=1,
            question_text=text,
            options=[{"key": k, "text": k} for k in "ABCD"],
            correct_answer="A",
            explanation="An explanation long enough to be useful to a student.",
            distractor_rationales={k: f"mistake {k}" for k in "BCD"},
        )

    sub_id = world["sub12"].id
    level = DifficultyLevel.BEGINNER

    assert _store(db, sub_id, level, question("First question?"), {}, "verified") is True
    # Same content again: rejected, but the first row must survive.
    assert _store(db, sub_id, level, question("First question?"), {}, "verified") is False
    assert _store(db, sub_id, level, question("Second question?"), {}, "verified") is True
    db.commit()

    assert db.query(QuestionBankItem).count() == 2


# --------------------------------------------------------------------------- #
# Lookahead: when the NEXT unit starts being pre-generated
# --------------------------------------------------------------------------- #


def _progress_at(
    db: Session, student_id: str, sub_unit: CurriculumSubUnit, levels: int
) -> SubUnitProgress:
    """Give a student ``levels`` completed tiers (0-3) on one sub-unit."""
    progress = SubUnitProgress(student_id=student_id, sub_unit_id=sub_unit.id)
    for level in list(DifficultyLevel)[:levels]:
        progress.mark_level_complete(level, 95.0)
    db.add(progress)
    db.flush()
    return progress


@pytest.fixture()
def wide_unit(db: Session, world: dict[str, Any]) -> list[CurriculumSubUnit]:
    """Unit 1 widened to five sub-units, so completion fractions are meaningful."""
    extra = []
    for index in (2, 3, 4):
        sub = CurriculumSubUnit(
            unit_id=world["unit1"].id,
            sub_unit_number=f"1.{index + 1}",
            sequence=index,
            title=f"Extra {index}",
            is_indexed=True,
        )
        db.add(sub)
        extra.append(sub)
    db.commit()
    return [world["sub11"], world["sub12"], *extra]


def test_unit_completion_is_a_fraction(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    from services.scheduler_jobs import unit_completion

    student_id = world["student"].id
    assert unit_completion(db, student_id, world["unit1"].id) == 0.0

    # Three of five sub-units fully done -> 60%.
    for sub in wide_unit[:3]:
        _progress_at(db, student_id, sub, levels=3)
    db.commit()
    assert unit_completion(db, student_id, world["unit1"].id) == pytest.approx(0.6)


def test_partial_tiers_count_proportionally(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    """A sub-unit at 67% should not count the same as one at 100%."""
    from services.scheduler_jobs import unit_completion

    student_id = world["student"].id
    _progress_at(db, student_id, wide_unit[0], levels=2)  # 67%
    db.commit()
    assert unit_completion(db, student_id, world["unit1"].id) == pytest.approx(0.134, abs=0.01)


def test_unit_one_is_always_in_scope(db: Session, world: dict[str, Any]) -> None:
    """A brand-new install with no students must still stock unit 1."""
    from services.scheduler_jobs import units_to_fill

    assert units_to_fill(db) == [1]


def test_next_unit_not_queued_early(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    """Starting unit 1 must not immediately pre-generate unit 2."""
    from services.scheduler_jobs import units_to_fill

    _progress_at(db, world["student"].id, wide_unit[0], levels=3)  # 20% of the unit
    db.commit()

    assert units_to_fill(db, trigger=0.7) == [1]


def test_next_unit_queued_near_the_end(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    """Four of five sub-units done (80%) should pull unit 2 into scope."""
    from services.scheduler_jobs import units_to_fill

    for sub in wide_unit[:4]:
        _progress_at(db, world["student"].id, sub, levels=3)
    db.commit()

    assert units_to_fill(db, trigger=0.7) == [1, 2]


def test_trigger_boundary_is_inclusive(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    from services.scheduler_jobs import units_to_fill

    for sub in wide_unit[:3]:
        _progress_at(db, world["student"].id, sub, levels=3)  # exactly 60%
    db.commit()

    assert units_to_fill(db, trigger=0.6) == [1, 2]
    assert units_to_fill(db, trigger=0.61) == [1]


def test_scope_never_runs_past_the_last_unit(db: Session, world: dict[str, Any]) -> None:
    """Finishing the final unit must not queue a unit that does not exist."""
    from services.scheduler_jobs import units_to_fill

    # world has units 1 and 2; finish unit 2 entirely.
    _progress_at(db, world["student"].id, world["sub21"], levels=3)
    db.commit()

    assert units_to_fill(db, trigger=0.7) == [1, 2]


def test_multiple_students_take_the_union(
    db: Session, world: dict[str, Any], wide_unit: list[CurriculumSubUnit]
) -> None:
    """One student far ahead should not stop another's unit being stocked."""
    from services.scheduler_jobs import units_to_fill

    ahead = Student(user_id=world["student"].user_id, first_name="Vihaan", grade_level=6)
    db.add(ahead)
    db.flush()

    # One student barely started unit 1; the other is deep into unit 2.
    _progress_at(db, world["student"].id, wide_unit[0], levels=1)
    _progress_at(db, ahead.id, world["sub21"], levels=3)
    db.commit()

    assert units_to_fill(db, trigger=0.7) == [1, 2]


def test_refill_respects_the_trigger(db: Session, world: dict[str, Any]) -> None:
    """The job should not even look at unit 2 while unit 1 is early."""
    from services.scheduler_jobs import units_to_fill

    _progress_at(db, world["student"].id, world["sub11"], levels=1)
    db.commit()

    scope = units_to_fill(db, trigger=0.7)
    assert 2 not in scope, "unit 2 pre-generated far too early"
