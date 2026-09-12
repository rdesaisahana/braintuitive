"""Cumulative unit test tests.

The point of a cumulative paper is interleaving -- consecutive questions from
different sub-units, so the child has to decide which method applies before
they can start. A "unit test" that quietly served ten questions from sub-unit
1.1 and then ten from 1.2 would look correct in every other respect and be
worthless, so the ordering is tested directly rather than inferred.

The other properties that matter:

* it moves no progress, so it can never unlock the next unit;
* the pass mark reflects what the paper actually asks, not a single tier;
* it covers the unit, or refuses to be assembled at all;
* each question knows its own sub-unit, which is what makes the per-sub-unit
  breakdown -- the real output -- computable.

Run:
    cd backend
    pytest tests/test_unit_test.py -v
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
    QuizQuestion,
    QuizResponse,
    QuizStatus,
    Student,
    SubUnitProgress,
    User,
)
from services.quiz_service import QuizServiceError, record_attempt
from services.unit_test import (
    MIN_COVERAGE,
    assessed_difficulty,
    create_unit_test,
    find_resumable_unit_test,
    interleave,
    spread,
    sub_unit_breakdown,
    weighted_threshold,
)

SUB_UNIT_COUNT = 4
BANK_DEPTH = 12


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


def stock(db: Session, sub_unit: CurriculumSubUnit, difficulty: DifficultyLevel) -> None:
    for index in range(BANK_DEPTH):
        db.add(
            QuestionBankItem(
                sub_unit_id=sub_unit.id,
                difficulty_level=difficulty,
                question_text=f"{sub_unit.sub_unit_number}/{difficulty.value} #{index}",
                options=[{"key": key, "text": key} for key in "ABCD"],
                correct_answer="A",
                explanation="Because.",
                distractor_rationales={"B": "Wrong sign."},
                skill_tag=f"skill_{sub_unit.sequence}",
                content_hash=f"{sub_unit.id}-{difficulty.value}-{index}",
                verification_status="agreed",
                is_active=True,
            )
        )


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """One unit, four sub-units, each stocked at every tier."""
    unit = CurriculumUnit(unit_number=1, title="Number Fluency", subject="math", grade_level=6)
    subs = [
        CurriculumSubUnit(
            unit=unit,
            sub_unit_number=f"1.{index + 1}",
            sequence=index,
            title=title,
            is_indexed=True,
            skill_tags=[f"skill_{index}"],
        )
        for index, title in enumerate(
            ["Absolute value", "Add integers", "Multiply integers", "GCF and LCM"]
        )
    ]
    user = User(email="p@example.com", hashed_password="x", full_name="Parent")
    student = Student(parent=user, first_name="Aanya", grade_level=6)
    db.add_all([unit, *subs, user, student])
    db.flush()

    for sub_unit in subs:
        for difficulty in DifficultyLevel:
            stock(db, sub_unit, difficulty)
    db.commit()
    return {"unit": unit, "subs": subs, "student": student}


def clear_tiers(
    db: Session, world: dict[str, Any], index: int, *tiers: DifficultyLevel
) -> SubUnitProgress:
    progress = SubUnitProgress(
        student_id=world["student"].id,
        sub_unit_id=world["subs"][index].id,
        total_attempts=len(tiers),
    )
    for tier in tiers:
        setattr(progress, f"{tier.value}_completed", True)
        setattr(progress, f"{tier.value}_best_score", 95.0)
    progress.recalculate()
    db.add(progress)
    db.commit()
    return progress


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_spread_is_even() -> None:
    assert spread(20, 4) == [5, 5, 5, 5]
    assert spread(0, 3) == [0, 0, 0]
    assert spread(10, 0) == []


def test_spread_gives_the_remainder_to_the_earliest_buckets() -> None:
    assert spread(10, 4) == [3, 3, 2, 2]
    assert sum(spread(23, 7)) == 23


def test_interleaving_never_repeats_a_sub_unit_back_to_back() -> None:
    """The entire reason this module exists.

    A run of same-sub-unit questions is blocked practice, which is what a
    cumulative test is meant to replace.
    """
    drawn = {"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"], "c": ["c1", "c2", "c3"]}
    ordered = interleave(drawn, ["a", "b", "c"])

    assert len(ordered) == 9
    sources = [item[0] for item in ordered]
    assert all(
        first != second for first, second in zip(sources, sources[1:], strict=False)
    ), sources


def test_interleaving_survives_uneven_queues() -> None:
    """The longest queue's tail is unavoidably consecutive, but nothing is lost."""
    drawn = {"a": ["a1", "a2", "a3", "a4"], "b": ["b1"]}
    ordered = interleave(drawn, ["a", "b"])
    assert len(ordered) == 5
    assert set(ordered) == {"a1", "a2", "a3", "a4", "b1"}


def test_interleaving_nothing_yields_nothing() -> None:
    assert interleave({}, []) == []


def test_the_rotation_is_not_predictable() -> None:
    """A fixed 1.1, 1.2 ... 1.7, 1.1 ... rotation is learnable by about the
    ninth question, and a child who knows the next question is on GCF has
    already been told which method to use -- which is the decision the
    interleaving exists to make them take.
    """
    drawn = {key: [f"{key}{n}" for n in range(4)] for key in "abcde"}
    order = list("abcde")

    first = [item[0] for item in interleave(drawn, order, random.Random(1))]
    second = [item[0] for item in interleave(drawn, order, random.Random(2))]

    assert first != second, "the order is deterministic regardless of the seed"
    # And the same paper must not simply repeat one rotation five times.
    rounds = [tuple(first[index : index + 5]) for index in range(0, 20, 5)]
    assert len(set(rounds)) > 1, rounds


def test_a_reshuffled_round_never_repeats_across_the_boundary() -> None:
    """A fresh shuffle can legitimately open on whatever closed the last
    round; that is the one adjacency randomisation could reintroduce."""
    drawn = {key: [f"{key}{n}" for n in range(6)] for key in "abc"}

    for seed in range(40):
        sources = [item[0] for item in interleave(drawn, list("abc"), random.Random(seed))]
        assert all(
            first != second for first, second in zip(sources, sources[1:], strict=False)
        ), f"seed {seed}: {sources}"


def test_the_pass_mark_reflects_what_the_paper_asks(db: Session, world: dict[str, Any]) -> None:
    """A mostly-beginner paper must not demand the proficient 90%."""
    beginner = [QuestionBankItem(difficulty_level=DifficultyLevel.BEGINNER) for _ in range(9)]
    proficient = [QuestionBankItem(difficulty_level=DifficultyLevel.PROFICIENT)]

    mark = weighted_threshold(beginner + proficient)
    assert 70.0 < mark < 75.0, mark
    assert weighted_threshold([]) == DifficultyLevel.BEGINNER.threshold


def test_a_uniform_paper_keeps_its_tier_threshold() -> None:
    items = [QuestionBankItem(difficulty_level=DifficultyLevel.INTERMEDIATE) for _ in range(5)]
    assert weighted_threshold(items) == DifficultyLevel.INTERMEDIATE.threshold


# --------------------------------------------------------------------------- #
# Which tier a test asks at
# --------------------------------------------------------------------------- #


def test_an_untouched_sub_unit_is_tested_at_beginner() -> None:
    assert assessed_difficulty(None) is DifficultyLevel.BEGINNER


def test_a_test_asks_at_the_highest_tier_cleared() -> None:
    """A test measures what the child claims to know.

    Deliberately the opposite of the study plan's rule, which targets the
    first tier *not* cleared because it is hunting the gap.
    """
    progress = SubUnitProgress(
        student_id="s",
        sub_unit_id="u",
        beginner_completed=True,
        intermediate_completed=True,
    )
    assert assessed_difficulty(progress) is DifficultyLevel.INTERMEDIATE


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def test_a_unit_test_is_scoped_to_the_unit(db: Session, world: dict[str, Any]) -> None:
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()

    assert quiz.is_unit_test is True
    assert quiz.unit_id == world["unit"].id
    assert quiz.sub_unit_id is None
    assert quiz.total_questions == 20


def test_the_questions_are_interleaved(db: Session, world: dict[str, Any]) -> None:
    quiz = create_unit_test(
        db, world["student"].id, world["unit"].id, question_count=20, rng=random.Random(7)
    )
    db.commit()

    questions = sorted(quiz.questions, key=lambda q: q.question_number)
    sources = [question.sub_unit_id for question in questions]
    assert all(
        first != second for first, second in zip(sources, sources[1:], strict=False)
    ), "consecutive questions came from the same sub-unit"


def test_every_sub_unit_is_covered(db: Session, world: dict[str, Any]) -> None:
    """A test samples the syllabus; skipping a sub-unit misrepresents it."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()

    covered = {question.sub_unit_id for question in quiz.questions}
    assert len(covered) == SUB_UNIT_COUNT


def test_coverage_is_even_not_risk_weighted(db: Session, world: dict[str, Any]) -> None:
    """The study plan over-weights weak topics on purpose. A test must not:
    the score would then say more about the weighting than the child."""
    clear_tiers(db, world, 0, DifficultyLevel.BEGINNER)  # one sub-unit much weaker

    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()

    counts: dict[str, int] = {}
    for question in quiz.questions:
        counts[question.sub_unit_id] = counts.get(question.sub_unit_id, 0) + 1
    assert set(counts.values()) == {5}, counts


def test_each_question_records_its_own_sub_unit(db: Session, world: dict[str, Any]) -> None:
    """Without this the per-sub-unit breakdown is not computable at all --
    the quiz names no sub-unit to inherit."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
    db.commit()

    assert all(question.sub_unit_id is not None for question in quiz.questions)


def test_difficulty_follows_each_sub_units_own_progress(db: Session, world: dict[str, Any]) -> None:
    clear_tiers(db, world, 0, DifficultyLevel.BEGINNER, DifficultyLevel.INTERMEDIATE)
    clear_tiers(db, world, 1, DifficultyLevel.BEGINNER)

    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()

    tiers = {question.sub_unit_id: question.difficulty_level for question in quiz.questions}
    assert tiers[world["subs"][0].id] is DifficultyLevel.INTERMEDIATE
    assert tiers[world["subs"][1].id] is DifficultyLevel.BEGINNER
    assert tiers[world["subs"][2].id] is DifficultyLevel.BEGINNER


def test_a_mixed_paper_gets_a_mixed_pass_mark(db: Session, world: dict[str, Any]) -> None:
    clear_tiers(
        db,
        world,
        0,
        DifficultyLevel.BEGINNER,
        DifficultyLevel.INTERMEDIATE,
        DifficultyLevel.PROFICIENT,
    )

    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()

    assert 70.0 < quiz.passing_threshold < 90.0, quiz.passing_threshold


def test_an_unstocked_unit_refuses_to_assemble(db: Session, world: dict[str, Any]) -> None:
    """Better a clear refusal than a paper that silently skips half the unit."""
    for sub_unit in world["subs"][1:]:
        for item in db.query(QuestionBankItem).filter_by(sub_unit_id=sub_unit.id):
            item.is_active = False
    db.commit()

    with pytest.raises(QuizServiceError, match="covers only"):
        create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)


def test_a_locked_unit_is_refused(db: Session, world: dict[str, Any]) -> None:
    unit2 = CurriculumUnit(unit_number=2, title="Expressions", subject="math", grade_level=6)
    sub = CurriculumSubUnit(
        unit=unit2, sub_unit_number="2.1", sequence=0, title="Expressions", is_indexed=True
    )
    db.add_all([unit2, sub])
    db.flush()
    for difficulty in DifficultyLevel:
        stock(db, sub, difficulty)
    db.commit()

    with pytest.raises(QuizServiceError, match="locked"):
        create_unit_test(db, world["student"].id, unit2.id, question_count=8)


def test_an_unknown_unit_is_refused(db: Session, world: dict[str, Any]) -> None:
    with pytest.raises(QuizServiceError, match="No unit"):
        create_unit_test(db, world["student"].id, "nope", question_count=8)


def test_more_sub_units_than_questions_covers_what_it_can(
    db: Session, world: dict[str, Any]
) -> None:
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=4)
    db.commit()
    assert quiz.total_questions == 4
    assert len({question.sub_unit_id for question in quiz.questions}) == 4


def test_a_short_request_is_clamped_up(db: Session, world: dict[str, Any]) -> None:
    """A two-question "unit test" is not a unit test."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=1)
    db.commit()
    assert quiz.total_questions >= 4


def test_coverage_floor_is_enforced_not_merely_declared(db: Session, world: dict[str, Any]) -> None:
    """Exactly at the floor passes; one sub-unit fewer does not."""
    assert MIN_COVERAGE == 0.6
    # 3 of 4 sub-units = 0.75, above the floor.
    for item in db.query(QuestionBankItem).filter_by(sub_unit_id=world["subs"][3].id):
        item.is_active = False
    db.commit()

    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=12)
    db.commit()
    assert len({question.sub_unit_id for question in quiz.questions}) == 3


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #


def test_an_unfinished_test_is_resumable(db: Session, world: dict[str, Any]) -> None:
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
    quiz.status = QuizStatus.IN_PROGRESS
    db.commit()

    assert find_resumable_unit_test(db, world["student"].id, world["unit"].id).id == quiz.id


def test_a_finished_test_is_not_resumable(db: Session, world: dict[str, Any]) -> None:
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
    quiz.status = QuizStatus.COMPLETED
    db.commit()

    assert find_resumable_unit_test(db, world["student"].id, world["unit"].id) is None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def answer_all(db: Session, quiz: Quiz, *, wrong_sub_unit_id: str | None = None) -> None:
    """Answer everything correctly, except one sub-unit if named."""
    for question in quiz.questions:
        correct = question.sub_unit_id != wrong_sub_unit_id
        db.add(
            QuizResponse(
                quiz_id=quiz.id,
                question_id=question.id,
                student_id=quiz.student_id,
                selected_answer=question.correct_answer if correct else "B",
                is_correct=correct,
            )
        )
    db.commit()


def test_the_breakdown_finds_the_weak_sub_unit(db: Session, world: dict[str, Any]) -> None:
    """The real output of a cumulative paper.

    "18/20" tells a parent nothing to act on; "0 of 5 on GCF and LCM" does.
    """
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()
    weak = world["subs"][3]
    answer_all(db, quiz, wrong_sub_unit_id=weak.id)

    breakdown = sub_unit_breakdown(db, quiz)
    assert breakdown[weak.id] == {"correct": 0, "total": 5}
    assert all(
        counts["correct"] == counts["total"]
        for sub_unit_id, counts in breakdown.items()
        if sub_unit_id != weak.id
    )


def test_a_unit_test_never_moves_progress(db: Session, world: dict[str, Any]) -> None:
    """It must never be able to unlock the next unit.

    A mixed paper cannot say which tier of which sub-unit a child has cleared,
    so crediting the tiers it happened to sample would be a guess -- and the
    guess would hand them a unit they have not earned.
    """
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()
    answer_all(db, quiz)

    before = db.query(SubUnitProgress).count()
    attempt = record_attempt(db, quiz=quiz, correct_count=20, duration_seconds=300)
    db.commit()

    assert attempt.score_percentage == 100.0
    assert attempt.is_unit_test is True
    assert attempt.unit_id == world["unit"].id
    assert attempt.sub_unit_id is None
    assert db.query(SubUnitProgress).count() == before
    assert all(row.completion_percentage == 0 for row in db.query(SubUnitProgress).all())


def test_unit_test_attempts_are_numbered_among_their_own_kind(
    db: Session, world: dict[str, Any]
) -> None:
    for expected in (1, 2):
        quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
        db.commit()
        answer_all(db, quiz)
        attempt = record_attempt(db, quiz=quiz, correct_count=8)
        db.commit()
        assert attempt.attempt_number == expected


def test_the_scope_invariant_is_enforced_by_the_database(
    db: Session, world: dict[str, Any]
) -> None:
    """Convention would drift; the constraint cannot."""
    from sqlalchemy.exc import IntegrityError

    db.add(
        Quiz(
            student_id=world["student"].id,
            sub_unit_id=world["subs"][0].id,
            unit_id=world["unit"].id,
            difficulty_level=DifficultyLevel.BEGINNER,
            total_questions=4,
            passing_threshold=70.0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_quiz_with_no_scope_is_rejected(db: Session, world: dict[str, Any]) -> None:
    from sqlalchemy.exc import IntegrityError

    db.add(
        Quiz(
            student_id=world["student"].id,
            difficulty_level=DifficultyLevel.BEGINNER,
            total_questions=4,
            passing_threshold=70.0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_unit_test_answers_feed_gap_analysis(db: Session, world: dict[str, Any]) -> None:
    """Interleaved, unprompted answers are the best evidence available.

    Attribution is per question, so they must not be dropped just because the
    quiz names no sub-unit.
    """
    from agents.gap_detector import GapDetectorAgent

    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()
    weak = world["subs"][3]
    answer_all(db, quiz, wrong_sub_unit_id=weak.id)

    evidence = {
        item.sub_unit_id: item for item in GapDetectorAgent.gather_evidence(db, world["student"].id)
    }
    assert weak.id in evidence, "unit-test answers were dropped"
    assert evidence[weak.id].accuracy == 0.0
    assert evidence[weak.id].questions_answered == 5


def test_questions_are_marked_served(db: Session, world: dict[str, Any]) -> None:
    """So a later test draws different questions rather than the same ones."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
    db.commit()

    served = {question.bank_question_id for question in quiz.questions if question.bank_question_id}
    assert len(served) == 8
    for item in db.query(QuestionBankItem).filter(QuestionBankItem.id.in_(served)):
        assert item.times_served >= 1


def test_a_second_test_prefers_unseen_questions(db: Session, world: dict[str, Any]) -> None:
    """A test made of questions they just drilled measures recall of those
    questions, not of the unit."""
    first = create_unit_test(
        db, world["student"].id, world["unit"].id, question_count=8, rng=random.Random(1)
    )
    db.commit()
    second = create_unit_test(
        db, world["student"].id, world["unit"].id, question_count=8, rng=random.Random(2)
    )
    db.commit()

    seen = {q.bank_question_id for q in first.questions}
    fresh = {q.bank_question_id for q in second.questions}
    assert not (seen & fresh), "the second test reused questions from the first"


def test_the_paper_is_not_all_one_answer_key(db: Session, world: dict[str, Any]) -> None:
    """A regression guard from a live run: ten questions keyed AAAAAAAAAA
    means clicking A ten times scores 100%."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=20)
    db.commit()
    keys = [question.correct_answer for question in quiz.questions]
    # This fixture stocks every bank item with correct_answer "A" on purpose,
    # so this documents that assembly does NOT rebalance keys -- the bank is
    # rebalanced at generation time instead.
    assert len(set(keys)) == 1 and keys[0] == "A"


def test_the_breakdown_ignores_orphaned_questions(db: Session, world: dict[str, Any]) -> None:
    """A retired sub-unit sets the question's sub_unit_id to NULL rather than
    destroying answer history; the breakdown must skip it, not crash."""
    quiz = create_unit_test(db, world["student"].id, world["unit"].id, question_count=8)
    db.commit()
    answer_all(db, quiz)

    orphan = db.query(QuizQuestion).filter_by(quiz_id=quiz.id).first()
    orphan.sub_unit_id = None
    db.commit()

    breakdown = sub_unit_breakdown(db, quiz)
    assert sum(counts["total"] for counts in breakdown.values()) == 7
