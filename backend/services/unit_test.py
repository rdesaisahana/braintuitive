"""Cumulative unit tests: one paper covering a whole unit.

Every other quiz in this system is blocked practice -- ten questions on one
sub-unit, one difficulty, all in a row. That is the right shape for learning a
skill and the wrong shape for finding out whether it stuck. A child who has
just read the worked example gets the next ten right without ever having to
decide *which* method applies.

A cumulative test interleaves instead: consecutive questions come from
different sub-units, so each one begins with the question a real test asks --
what kind of problem is this? Interleaved practice reliably scores worse in
the session and better weeks later, which is exactly the trade a test wants.

Two rules distinguish this from the revision plan the Test Prep agent builds:

* **Coverage is even, not risk-weighted.** A test samples the syllabus. The
  study plan deliberately over-weights the shaky topics, because its job is to
  fix them; a test that did the same would report a score that says more about
  the weighting than the child.
* **Difficulty follows what the child has already cleared**, per sub-unit, so
  the paper asks at the level they claim to have reached. The study plan does
  the opposite and targets the first tier they have *not* cleared, because it
  is looking for the gap rather than measuring the whole.

A unit test never moves progress. Sub-unit progress is earned by clearing that
sub-unit's three tiers, and a mixed paper cannot say which of them a child has
cleared -- so the schema keeps ``unit_id`` and ``sub_unit_id`` mutually
exclusive rather than leaving that to convention.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    QuestionBankItem,
    Quiz,
    QuizQuestion,
    QuizStatus,
    Student,
    SubUnitProgress,
)
from services.question_bank import mark_served
from services.quiz_service import QuizServiceError, check_unit_unlocked

logger = logging.getLogger(__name__)

DEFAULT_TEST_LENGTH = 20
MIN_TEST_LENGTH = 4
MAX_TEST_LENGTH = 60
# A test that cannot cover most of the unit is not a unit test. Below this
# fraction of sub-units represented, assembly fails loudly rather than handing
# a child a paper that silently skips half the syllabus.
MIN_COVERAGE = 0.6


@dataclass
class UnitTestBlueprint:
    """What a cumulative test will contain, before anything is persisted."""

    unit_id: str
    unit_number: int
    # sub_unit_id -> (difficulty, how many questions)
    allocation: dict[str, tuple[DifficultyLevel, int]] = field(default_factory=dict)
    sub_units_covered: int = 0
    sub_units_total: int = 0
    total_questions: int = 0
    passing_threshold: float = 0.0

    @property
    def coverage(self) -> float:
        return self.sub_units_covered / self.sub_units_total if self.sub_units_total else 0.0


def assessed_difficulty(progress: SubUnitProgress | None) -> DifficultyLevel:
    """The tier a test should ask this sub-unit at.

    The highest tier the child has actually cleared: a test measures what they
    claim to know. This is deliberately the opposite of the study plan's rule,
    which targets the first tier *not* cleared because it is hunting the gap.
    """
    if progress is None:
        return DifficultyLevel.BEGINNER
    for level in (
        DifficultyLevel.PROFICIENT,
        DifficultyLevel.INTERMEDIATE,
        DifficultyLevel.BEGINNER,
    ):
        if getattr(progress, f"{level.value}_completed", False):
            return level
    return DifficultyLevel.BEGINNER


def spread(total: int, buckets: int) -> list[int]:
    """Split ``total`` as evenly as possible into ``buckets`` parts.

    The remainder goes to the earliest buckets, which are the earliest
    sub-units -- foundational material, so the tie-break is at worst harmless.
    """
    if buckets <= 0:
        return []
    base, extra = divmod(total, buckets)
    return [base + (1 if index < extra else 0) for index in range(buckets)]


def interleave(
    drawn: dict[str, list[QuestionBankItem]],
    order: list[str],
    rng: random.Random | None = None,
) -> list[QuestionBankItem]:
    """Order questions so consecutive ones come from different sub-units.

    Round-robin rather than a plain shuffle: a shuffle leaves runs of the same
    sub-unit often enough to matter, and a run is exactly the blocked practice
    this module exists to avoid.

    The order within each round is itself reshuffled. A fixed rotation --
    1.1, 1.2 ... 1.7, 1.1 ... -- is learnable by about the ninth question, and
    a child who knows the next question is on GCF has already been told which
    method to use. That is the decision the interleaving is there to make them
    take, so handing it back would quietly undo the point.

    Adjacency across a round boundary is checked explicitly, because a fresh
    shuffle can legitimately start with whatever the previous round ended on.
    """
    rng = rng or random.Random()
    queues = {key: list(items) for key, items in drawn.items()}
    ordered: list[QuestionBankItem] = []
    previous: str | None = None

    while any(queues.values()):
        live = [key for key in order if queues.get(key)]
        rng.shuffle(live)
        # Never open a round on the sub-unit that closed the last one.
        if len(live) > 1 and live[0] == previous:
            live[0], live[1] = live[1], live[0]

        for key in live:
            ordered.append(queues[key].pop(0))
            previous = key

    return ordered


def weighted_threshold(items: list[QuestionBankItem]) -> float:
    """Pass mark for a mixed paper: the mean of its questions' own thresholds.

    A test that is mostly beginner questions must not demand the proficient
    90%, and one that is mostly proficient must not pass at the beginner 70%.
    Taking the quiz-level tier as the threshold would do one or the other.
    """
    if not items:
        return DifficultyLevel.BEGINNER.threshold
    return round(sum(item.difficulty_level.threshold for item in items) / len(items), 2)


def _headline_difficulty(items: list[QuestionBankItem]) -> DifficultyLevel:
    """The tier to label a mixed paper with -- the most represented one.

    Only a label. ``passing_threshold`` carries the arithmetic that matters.
    """
    if not items:
        return DifficultyLevel.BEGINNER
    counts: dict[DifficultyLevel, int] = {}
    for item in items:
        counts[item.difficulty_level] = counts.get(item.difficulty_level, 0) + 1
    return max(counts.items(), key=lambda pair: (pair[1], pair[0].order))[0]


def plan_unit_test(
    db: Session,
    student_id: str,
    unit: CurriculumUnit,
    question_count: int = DEFAULT_TEST_LENGTH,
) -> UnitTestBlueprint:
    """Decide the shape of the paper: which sub-units, which tiers, how many."""
    sub_units = [sub for sub in unit.sub_units if sub.is_active]
    sub_units.sort(key=lambda sub: sub.sequence)
    if not sub_units:
        raise QuizServiceError(f"Unit {unit.unit_number} has no active sub-units.")

    progress = {
        row.sub_unit_id: row
        for row in db.query(SubUnitProgress).filter_by(student_id=student_id).all()
    }

    # More sub-units than questions would give some of them zero, so the paper
    # covers as many as it can afford at one question each.
    affordable = min(len(sub_units), question_count)
    chosen = sub_units[:affordable]
    counts = spread(question_count, len(chosen))

    blueprint = UnitTestBlueprint(
        unit_id=unit.id,
        unit_number=unit.unit_number,
        sub_units_total=len(sub_units),
        allocation={
            sub.id: (assessed_difficulty(progress.get(sub.id)), count)
            for sub, count in zip(chosen, counts, strict=True)
            if count > 0
        },
    )
    blueprint.sub_units_covered = len(blueprint.allocation)
    blueprint.total_questions = sum(count for _, count in blueprint.allocation.values())
    return blueprint


def _draw(
    db: Session,
    student_id: str,
    blueprint: UnitTestBlueprint,
    rng: random.Random,
) -> dict[str, list[QuestionBankItem]]:
    """Pull the blueprint's questions out of the bank, preferring unseen ones.

    Questions already served to this student in an ordinary quiz are avoided
    where possible: a test made of questions they have just drilled measures
    recall of those questions, not of the unit.
    """
    drawn: dict[str, list[QuestionBankItem]] = {}

    for sub_unit_id, (difficulty, count) in blueprint.allocation.items():
        available = (
            db.query(QuestionBankItem)
            .filter_by(sub_unit_id=sub_unit_id, difficulty_level=difficulty, is_active=True)
            .filter(QuestionBankItem.verification_status != "disputed")
            .all()
        )
        if not available:
            continue

        seen = {
            row[0]
            for row in db.query(QuizQuestion.bank_question_id)
            .join(Quiz, Quiz.id == QuizQuestion.quiz_id)
            .filter(
                Quiz.student_id == student_id,
                QuizQuestion.bank_question_id.is_not(None),
            )
            .all()
            if row[0]
        }

        unseen = [item for item in available if item.id not in seen]
        rng.shuffle(unseen)
        picked = unseen[:count]

        if len(picked) < count:
            repeats = [item for item in available if item.id in seen]
            rng.shuffle(repeats)
            picked.extend(repeats[: count - len(picked)])

        if picked:
            drawn[sub_unit_id] = picked

    return drawn


def create_unit_test(
    db: Session,
    student_id: str,
    unit_id: str,
    question_count: int = DEFAULT_TEST_LENGTH,
    rng: random.Random | None = None,
    enforce_unlock: bool = True,
) -> Quiz:
    """Assemble and persist an interleaved cumulative test over one unit.

    Args:
        db: Active session. The caller commits.
        student_id: Student sitting the test.
        unit_id: Unit the test covers.
        question_count: Length of the paper, clamped to a sane range.
        rng: Injectable randomness, so a test run is reproducible.
        enforce_unlock: Set False only for seeding or admin tooling.

    Returns:
        The persisted :class:`Quiz`, scoped to the unit rather than a sub-unit.

    Raises:
        QuizServiceError: The unit is missing or locked, or the bank cannot
            cover enough of the unit to make the paper meaningful.
    """
    rng = rng or random.Random()
    wanted = max(MIN_TEST_LENGTH, min(question_count, MAX_TEST_LENGTH))

    student = db.get(Student, student_id)
    if student is None:
        raise QuizServiceError(f"No student with id {student_id!r}.")

    unit = db.get(CurriculumUnit, unit_id)
    if unit is None:
        raise QuizServiceError(f"No unit with id {unit_id!r}.")

    if enforce_unlock:
        unlock = check_unit_unlocked(db, student_id, unit)
        if not unlock.unlocked:
            raise QuizServiceError(unlock.reason or f"Unit {unit.unit_number} is locked.")

    blueprint = plan_unit_test(db, student_id, unit, wanted)
    drawn = _draw(db, student_id, blueprint, rng)

    covered = len(drawn)
    if covered < MIN_COVERAGE * blueprint.sub_units_total:
        raise QuizServiceError(
            f"The question bank covers only {covered} of "
            f"{blueprint.sub_units_total} sub-unit(s) in Unit {unit.unit_number}; "
            "a cumulative test needs most of the unit stocked."
        )

    order = [sub_unit_id for sub_unit_id in blueprint.allocation if sub_unit_id in drawn]
    items = interleave(drawn, order, rng)
    if len(items) < MIN_TEST_LENGTH:
        raise QuizServiceError(
            f"Only {len(items)} question(s) available for Unit {unit.unit_number}."
        )

    quiz = Quiz(
        student_id=student_id,
        sub_unit_id=None,
        unit_id=unit.id,
        difficulty_level=_headline_difficulty(items),
        status=QuizStatus.PENDING,
        total_questions=len(items),
        passing_threshold=weighted_threshold(items),
        is_practice=False,
        is_unit_test=True,
        generation_metadata={
            "source": "question_bank",
            "is_unit_test": True,
            "sub_units_covered": covered,
            "sub_units_total": blueprint.sub_units_total,
            "bank_question_ids": [item.id for item in items],
        },
    )
    db.add(quiz)
    db.flush()

    for number, item in enumerate(items, start=1):
        db.add(QuizQuestion(quiz_id=quiz.id, question_number=number, **item.to_question_kwargs()))

    mark_served(db, items)
    db.flush()

    logger.info(
        "Unit test %s assembled: %d question(s) across %d/%d sub-unit(s) of Unit %d, "
        "pass mark %.1f%%.",
        quiz.id,
        len(items),
        covered,
        blueprint.sub_units_total,
        unit.unit_number,
        quiz.passing_threshold,
    )
    return quiz


def find_resumable_unit_test(
    db: Session, student_id: str, unit_id: str | None = None
) -> Quiz | None:
    """An unfinished unit test to send the student back into.

    Same promise as an ordinary quiz: a child who closed the laptop mid-paper
    returns to the paper, not to the start of it.
    """
    query = db.query(Quiz).filter(
        Quiz.student_id == student_id,
        Quiz.is_unit_test.is_(True),
        Quiz.status.in_([QuizStatus.PENDING, QuizStatus.IN_PROGRESS]),
    )
    if unit_id is not None:
        query = query.filter(Quiz.unit_id == unit_id)
    return query.order_by(Quiz.created_at.desc()).first()


def sub_unit_breakdown(db: Session, quiz: Quiz) -> dict[str, dict[str, int]]:
    """Per-sub-unit correct/total for a finished unit test.

    The reason for sitting a cumulative paper: not the overall score but which
    parts of the unit it exposes. Reads ``QuizQuestion.sub_unit_id``, which is
    why that column exists -- the quiz itself names no sub-unit.
    """
    from db.models import QuizResponse

    rows = (
        db.query(QuizQuestion.sub_unit_id, QuizResponse.is_correct)
        .join(QuizResponse, QuizResponse.question_id == QuizQuestion.id)
        .filter(QuizQuestion.quiz_id == quiz.id)
        .all()
    )

    breakdown: dict[str, dict[str, int]] = {}
    for sub_unit_id, is_correct in rows:
        if sub_unit_id is None:
            continue
        bucket = breakdown.setdefault(sub_unit_id, {"correct": 0, "total": 0})
        bucket["total"] += 1
        bucket["correct"] += int(bool(is_correct))

    return breakdown


def sub_unit_titles(db: Session, sub_unit_ids: list[str]) -> dict[str, CurriculumSubUnit]:
    """Look up the sub-units a breakdown refers to, in one query."""
    if not sub_unit_ids:
        return {}
    return {
        sub_unit.id: sub_unit
        for sub_unit in db.query(CurriculumSubUnit)
        .filter(CurriculumSubUnit.id.in_(sub_unit_ids))
        .all()
    }


__all__ = [
    "DEFAULT_TEST_LENGTH",
    "MAX_TEST_LENGTH",
    "MIN_COVERAGE",
    "MIN_TEST_LENGTH",
    "UnitTestBlueprint",
    "assessed_difficulty",
    "create_unit_test",
    "find_resumable_unit_test",
    "interleave",
    "plan_unit_test",
    "spread",
    "sub_unit_breakdown",
    "sub_unit_titles",
    "weighted_threshold",
]
