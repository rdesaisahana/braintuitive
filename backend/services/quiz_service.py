"""Quiz business logic: prerequisites, persistence and scoring.

Sits between the Quiz Generator agent and the API routes. Owns the rules that
must hold no matter which route is calling:

* **Sequential units.** Unit N is locked until every sub-unit of Unit N-1 is at
  100%. Enforced here, not in a route handler, so a new endpoint cannot
  accidentally bypass it.
* **Flexible sub-units.** Within an unlocked unit, any sub-unit may be started
  in any order.
* **Provenance.** Every persisted question keeps the chunk ids it was generated
  from, and the quiz keeps the model and retrieval parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from agents.quiz_generator import GeneratedQuiz, QuizGeneratorAgent
from config import settings
from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizResponse,
    QuizStatus,
    Student,
    SubUnitProgress,
)
from services.question_bank import draw_questions, mark_served

logger = logging.getLogger(__name__)


class QuizServiceError(RuntimeError):
    """Raised when a quiz cannot be created for a business-rule reason."""


class UnitLockedError(QuizServiceError):
    """Raised when a student attempts a unit they have not unlocked."""


@dataclass
class UnlockStatus:
    """Whether a unit is available to a student, and why."""

    unlocked: bool
    unit_number: int
    reason: str = ""
    blocking_sub_units: list[str] | None = None


# --------------------------------------------------------------------------- #
# Progression rules
# --------------------------------------------------------------------------- #


def compute_unlock_status(
    unit: CurriculumUnit,
    previous_unit: CurriculumUnit | None,
    previous_sub_units: list[CurriculumSubUnit],
    completed_sub_unit_ids: set[str],
) -> UnlockStatus:
    """The sequential-unit rule, as a pure function.

    Kept separate from :func:`check_unit_unlocked` so the dashboard can apply
    the identical rule to data it has already batch-loaded. Two hand-written
    copies of a progression rule drift, and the direction they drift is a
    child being let into a unit they have not earned.
    """
    if unit.unit_number <= 1 or previous_unit is None or not previous_sub_units:
        return UnlockStatus(unlocked=True, unit_number=unit.unit_number)

    blocking = [
        sub.sub_unit_number for sub in previous_sub_units if sub.id not in completed_sub_unit_ids
    ]
    if blocking:
        return UnlockStatus(
            unlocked=False,
            unit_number=unit.unit_number,
            reason=(
                f"Unit {unit.unit_number} is locked until Unit {previous_unit.unit_number} "
                f"is fully complete ({len(blocking)} sub-unit(s) remaining)."
            ),
            blocking_sub_units=sorted(blocking),
        )
    return UnlockStatus(unlocked=True, unit_number=unit.unit_number)


def check_unit_unlocked(db: Session, student_id: str, unit: CurriculumUnit) -> UnlockStatus:
    """Determine whether a student may work in ``unit``.

    Unit 1 is always open. Unit N requires every sub-unit of Unit N-1 to be at
    100% -- that is, all three difficulty tiers passed.

    Returns:
        An :class:`UnlockStatus` naming the sub-units still blocking, so the UI
        can tell the student exactly what to finish.
    """
    if unit.unit_number <= 1:
        return UnlockStatus(unlocked=True, unit_number=unit.unit_number)

    # Scoped to the same owner: gating Unit 3 of an uploaded curriculum on
    # Unit 2 of the shared sample would be gating on a course the child has
    # never seen, and would unlock or lock it for the wrong reasons.
    previous = (
        db.query(CurriculumUnit)
        .filter_by(
            user_id=unit.user_id,
            subject=unit.subject,
            grade_level=unit.grade_level,
            unit_number=unit.unit_number - 1,
        )
        .one_or_none()
    )
    if previous is None:
        # No previous unit exists in this curriculum; nothing to gate on.
        return UnlockStatus(unlocked=True, unit_number=unit.unit_number)

    previous_sub_units = (
        db.query(CurriculumSubUnit).filter_by(unit_id=previous.id, is_active=True).all()
    )
    if not previous_sub_units:
        return UnlockStatus(unlocked=True, unit_number=unit.unit_number)

    completed = {
        progress.sub_unit_id
        for progress in db.query(SubUnitProgress)
        .filter(
            SubUnitProgress.student_id == student_id,
            SubUnitProgress.sub_unit_id.in_([s.id for s in previous_sub_units]),
            SubUnitProgress.completion_percentage == 100,
        )
        .all()
    }

    return compute_unlock_status(unit, previous, previous_sub_units, completed)


def get_or_create_progress(db: Session, student_id: str, sub_unit_id: str) -> SubUnitProgress:
    """Fetch a student's progress row for a sub-unit, creating it if absent."""
    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student_id, sub_unit_id=sub_unit_id)
        .one_or_none()
    )
    if progress is None:
        progress = SubUnitProgress(student_id=student_id, sub_unit_id=sub_unit_id)
        progress.first_started_at = datetime.now(UTC)
        db.add(progress)
        db.flush()
    return progress


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def find_resumable_quiz(
    db: Session,
    student_id: str,
    sub_unit_id: str | None = None,
    difficulty: DifficultyLevel | None = None,
) -> Quiz | None:
    """Return an unfinished quiz this student can pick up again.

    A child who closes the tab mid-quiz and comes back tomorrow must land back
    on the same questions. Without this, ``create_quiz`` would mint a fresh
    quiz: their answers are stranded on an orphaned row, and ten more bank
    questions are consumed for nothing.

    "Unfinished" means not completed and not abandoned. A quiz with every
    question answered but never submitted still counts -- the child needs to
    get back to it to press the button.

    Args:
        student_id: Whose quiz.
        sub_unit_id: Narrow to one sub-unit, or None for any.
        difficulty: Narrow to one tier, or None for any.

    Returns:
        The most recently started unfinished quiz, or None.
    """
    query = db.query(Quiz).filter(
        Quiz.student_id == student_id,
        Quiz.status.in_([QuizStatus.PENDING, QuizStatus.IN_PROGRESS]),
    )
    if sub_unit_id is not None:
        query = query.filter(Quiz.sub_unit_id == sub_unit_id)
    if difficulty is not None:
        query = query.filter(Quiz.difficulty_level == difficulty)

    # Most recent first: if several were abandoned, the latest is the one the
    # child was actually working on.
    for quiz in query.order_by(Quiz.created_at.desc()).all():
        if quiz.total_questions > 0:
            return quiz
    return None


def answered_question_ids(db: Session, quiz_id: str) -> set[str]:
    """Ids of the questions already answered in a quiz."""
    return {row[0] for row in db.query(QuizResponse.question_id).filter_by(quiz_id=quiz_id).all()}


def persist_quiz(
    db: Session,
    student_id: str,
    generated: GeneratedQuiz,
) -> Quiz:
    """Write a generated question set to the database.

    Args:
        db: Active session. The caller commits.
        student_id: Who the quiz is for.
        generated: The validated output of the Quiz Generator agent.

    Returns:
        The persisted :class:`Quiz`, with its questions attached.
    """
    difficulty = generated.difficulty
    quiz = Quiz(
        student_id=student_id,
        sub_unit_id=generated.sub_unit_id,
        difficulty_level=difficulty,
        status=QuizStatus.PENDING,
        total_questions=len(generated.questions),
        passing_threshold=difficulty.threshold,
        generation_metadata=generated.generation_metadata(),
    )
    db.add(quiz)
    db.flush()

    for question in generated.questions:
        db.add(QuizQuestion(quiz_id=quiz.id, **question.to_model_kwargs(difficulty)))

    db.flush()
    logger.info(
        "Persisted quiz %s: %d question(s), sub-unit %s, %s.",
        quiz.id,
        len(generated.questions),
        generated.sub_unit_number,
        difficulty.value,
    )
    return quiz


def _is_already_completed(
    db: Session, student_id: str, sub_unit_id: str, difficulty: DifficultyLevel
) -> bool:
    """True if this student has already passed this difficulty tier."""
    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student_id, sub_unit_id=sub_unit_id)
        .one_or_none()
    )
    if progress is None:
        return False
    return bool(getattr(progress, f"{difficulty.value}_completed", False))


def _create_from_bank(
    db: Session,
    student_id: str,
    sub_unit: CurriculumSubUnit,
    difficulty: DifficultyLevel,
    is_practice: bool,
) -> Quiz | None:
    """Assemble a quiz by copying questions out of the bank.

    Returns None when the bank cannot supply a full quiz, leaving the caller to
    fall back to live generation. A short quiz would quietly change the pass
    threshold arithmetic, so a partial draw is treated as no draw.
    """
    wanted = settings.QUESTIONS_PER_QUIZ
    items, all_fresh = draw_questions(db, student_id, sub_unit.id, difficulty, count=wanted)
    if len(items) < wanted:
        return None

    quiz = Quiz(
        student_id=student_id,
        sub_unit_id=sub_unit.id,
        difficulty_level=difficulty,
        status=QuizStatus.PENDING,
        total_questions=len(items),
        passing_threshold=difficulty.threshold,
        is_practice=is_practice,
        generation_metadata={
            "source": "question_bank",
            "all_questions_fresh": all_fresh,
            "bank_question_ids": [item.id for item in items],
            "is_practice": is_practice,
        },
    )
    db.add(quiz)
    db.flush()

    for number, item in enumerate(items, start=1):
        db.add(QuizQuestion(quiz_id=quiz.id, question_number=number, **item.to_question_kwargs()))

    mark_served(db, items)
    db.flush()

    logger.info(
        "Quiz %s assembled from bank: %d question(s), sub-unit %s, %s%s%s.",
        quiz.id,
        len(items),
        sub_unit.sub_unit_number,
        difficulty.value,
        "" if all_fresh else " (some repeats)",
        " [practice]" if is_practice else "",
    )
    return quiz


def create_quiz(
    db: Session,
    student_id: str,
    sub_unit_id: str,
    difficulty: DifficultyLevel | str,
    agent: QuizGeneratorAgent | None = None,
    enforce_unlock: bool = True,
    use_bank: bool = True,
) -> Quiz:
    """Generate and persist a quiz, enforcing progression rules.

    Args:
        db: Active session. The caller commits.
        student_id: Student the quiz belongs to.
        sub_unit_id: Sub-unit being quizzed.
        difficulty: Tier to generate.
        agent: Injectable generator, used when falling back to live generation.
        enforce_unlock: Set False only for seeding or admin tooling.
        use_bank: Draw from the pre-generated bank when it has stock. This is
            what keeps quiz creation at milliseconds instead of ~2 minutes;
            set False to force fresh generation.

    Returns:
        The persisted :class:`Quiz`.

    Raises:
        QuizServiceError: If the student or sub-unit does not exist.
        UnitLockedError: If the sub-unit's unit is not yet unlocked.
    """
    if isinstance(difficulty, str):
        difficulty = DifficultyLevel(difficulty.lower().strip())

    student = db.get(Student, student_id)
    if student is None:
        raise QuizServiceError(f"No student with id {student_id!r}.")

    sub_unit = db.get(CurriculumSubUnit, sub_unit_id)
    if sub_unit is None:
        raise QuizServiceError(f"No sub-unit with id {sub_unit_id!r}.")

    if enforce_unlock:
        status = check_unit_unlocked(db, student_id, sub_unit.unit)
        if not status.unlocked:
            raise UnitLockedError(status.reason)

    # A retry of a tier the student has already cleared is practice: recorded,
    # but never allowed to move progress in either direction.
    is_practice = _is_already_completed(db, student_id, sub_unit_id, difficulty)

    if use_bank:
        quiz = _create_from_bank(db, student_id, sub_unit, difficulty, is_practice)
        if quiz is not None:
            get_or_create_progress(db, student_id, sub_unit_id).last_activity_at = datetime.now(UTC)
            db.flush()
            return quiz
        logger.info(
            "Bank empty for %s/%s - falling back to live generation.",
            sub_unit.sub_unit_number,
            difficulty.value,
        )

    agent = agent or QuizGeneratorAgent()
    generated = agent.generate(
        sub_unit=sub_unit.id,
        difficulty=difficulty,
        count=settings.QUESTIONS_PER_QUIZ,
        db=db,
    )

    quiz = persist_quiz(db, student_id=student_id, generated=generated)
    quiz.is_practice = is_practice

    progress = get_or_create_progress(db, student_id, sub_unit_id)
    progress.last_activity_at = datetime.now(UTC)
    db.flush()

    return quiz


def record_attempt(
    db: Session,
    quiz: Quiz,
    correct_count: int,
    duration_seconds: int = 0,
    skill_breakdown: dict[str, float] | None = None,
) -> QuizAttempt:
    """Close out a quiz: score it, log the attempt, and move progress.

    Practice quizzes -- retries of a tier the student has already cleared --
    are scored and logged exactly like any other attempt, but deliberately do
    not touch ``SubUnitProgress``. The promise is that practising cannot cost
    you anything, and the cleanest way to keep that promise is to not write to
    progress at all rather than to rely on the "best score only" guards.

    Cumulative unit tests are excluded from progress too, for a different
    reason: they span a whole unit at mixed tiers, so there is no single
    ``(sub-unit, tier)`` for a score to belong to.

    Args:
        db: Active session. The caller commits.
        quiz: The quiz being completed.
        correct_count: How many questions the student got right.
        duration_seconds: Time spent on the quiz.
        skill_breakdown: Optional per-skill accuracy, for gap detection.

    Returns:
        The persisted :class:`QuizAttempt`.
    """
    quiz.correct_count = correct_count
    quiz.status = QuizStatus.COMPLETED
    quiz.completed_at = datetime.now(UTC)
    quiz.time_spent_seconds = duration_seconds
    quiz.compute_score()

    # A unit test is counted among its own kind, not among that unit's
    # sub-unit quizzes: "attempt 3" should mean the third sitting of the test.
    prior_query = db.query(QuizAttempt).filter_by(
        student_id=quiz.student_id, difficulty_level=quiz.difficulty_level
    )
    if quiz.is_unit_test:
        prior_query = prior_query.filter_by(unit_id=quiz.unit_id, is_unit_test=True)
    else:
        prior_query = prior_query.filter_by(sub_unit_id=quiz.sub_unit_id, is_unit_test=False)
    prior = prior_query.count()

    attempt = QuizAttempt(
        student_id=quiz.student_id,
        sub_unit_id=quiz.sub_unit_id,
        unit_id=quiz.unit_id,
        quiz_id=quiz.id,
        difficulty_level=quiz.difficulty_level,
        attempt_number=prior + 1,
        total_questions=quiz.total_questions,
        correct_count=correct_count,
        score_percentage=quiz.score_percentage or 0.0,
        is_passed=quiz.is_passed,
        is_practice=quiz.is_practice,
        is_unit_test=quiz.is_unit_test,
        skill_breakdown=skill_breakdown or {},
        duration_seconds=duration_seconds,
        started_at=quiz.started_at,
    )
    db.add(attempt)

    # A unit test moves no progress, for the same reason practice does not, but
    # a different one from practice's: sub-unit progress is earned by clearing
    # that sub-unit's three tiers, and a mixed paper cannot say which of them a
    # child has cleared. Crediting the tiers it happened to sample would let a
    # cumulative test unlock the next unit.
    if not quiz.is_practice and not quiz.is_unit_test:
        progress = get_or_create_progress(db, quiz.student_id, quiz.sub_unit_id)
        progress.total_attempts += 1
        progress.total_time_seconds += duration_seconds
        progress.record_score(quiz.difficulty_level, quiz.score_percentage or 0.0)

    db.flush()
    logger.info(
        "Attempt %d on %s: %.1f%% (%s)%s.",
        attempt.attempt_number,
        quiz.difficulty_level.value,
        attempt.score_percentage,
        "passed" if attempt.is_passed else "not passed",
        (
            " [practice - progress unchanged]"
            if quiz.is_practice
            else (" [unit test - progress unchanged]" if quiz.is_unit_test else "")
        ),
    )
    return attempt


__all__ = [
    "UnitLockedError",
    "UnlockStatus",
    "QuizServiceError",
    "check_unit_unlocked",
    "compute_unlock_status",
    "answered_question_ids",
    "create_quiz",
    "find_resumable_quiz",
    "record_attempt",
    "get_or_create_progress",
    "persist_quiz",
]
