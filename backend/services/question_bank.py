"""The pre-generated question bank.

Generation is the slow part of serving a quiz: roughly two minutes for a
verified set of ten. This module moves that cost off the request path. A
background job fills the bank ahead of where students are working, and
``create_quiz`` copies rows out of it in milliseconds.

Two rules govern the bank:

**Shared questions raise the stakes.** A defective item here reaches every
student who draws it, not one. So bank generation always runs with
verification on -- the extra cost is free in a background job -- and anything
the verifier disputes is stored ``is_active=False`` for review rather than
served.

**Retirement is soft.** Student answer history points at these rows. Deleting
a bad item would erase the record of what was actually asked, so items are
deactivated instead.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agents.quiz_generator import GeneratedQuestion, QuizGeneratorAgent
from config import settings
from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    QuestionBankItem,
    Quiz,
    QuizQuestion,
)

logger = logging.getLogger(__name__)

# How many questions each (sub-unit, difficulty) slot should hold.
DEFAULT_BANK_DEPTH = settings.BANK_DEPTH


def content_hash(question_text: str) -> str:
    """Stable hash of a question's text, for de-duplication within a slot."""
    normalised = re.sub(r"\s+", " ", (question_text or "").strip().lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass
class SlotStatus:
    """How well stocked one (sub-unit, difficulty) slot is."""

    sub_unit_id: str
    sub_unit_number: str
    unit_number: int
    difficulty: DifficultyLevel
    servable: int = 0
    quarantined: int = 0
    target: int = DEFAULT_BANK_DEPTH

    @property
    def shortfall(self) -> int:
        return max(0, self.target - self.servable)

    @property
    def is_stocked(self) -> bool:
        return self.shortfall == 0

    def __str__(self) -> str:
        return (
            f"{self.sub_unit_number}/{self.difficulty.value}: "
            f"{self.servable}/{self.target}"
            + (f" (+{self.quarantined} quarantined)" if self.quarantined else "")
        )


@dataclass
class FillReport:
    """What one bank-filling run accomplished."""

    slots_examined: int = 0
    slots_filled: int = 0
    questions_added: int = 0
    quarantined: int = 0
    duplicates_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"examined {self.slots_examined} slot(s), topped up {self.slots_filled}, "
            f"added {self.questions_added} question(s), "
            f"quarantined {self.quarantined}, skipped {self.duplicates_skipped} duplicate(s)"
            + (f", {len(self.errors)} error(s)" if self.errors else "")
        )


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #


def slot_status(
    db: Session,
    sub_unit: CurriculumSubUnit,
    difficulty: DifficultyLevel,
    target: int = DEFAULT_BANK_DEPTH,
) -> SlotStatus:
    """Return how many servable items one slot holds."""
    rows = (
        db.query(QuestionBankItem)
        .filter_by(sub_unit_id=sub_unit.id, difficulty_level=difficulty)
        .all()
    )
    return SlotStatus(
        sub_unit_id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        unit_number=sub_unit.unit.unit_number,
        difficulty=difficulty,
        servable=sum(1 for row in rows if row.is_servable),
        quarantined=sum(1 for row in rows if not row.is_servable),
        target=target,
    )


def bank_overview(
    db: Session,
    subject: str = "math",
    grade_level: int = 6,
    target: int = DEFAULT_BANK_DEPTH,
    max_unit: int | None = None,
) -> list[SlotStatus]:
    """Return the status of every slot, ordered by unit then sub-unit."""
    query = (
        db.query(CurriculumSubUnit)
        .join(CurriculumUnit)
        .filter(
            CurriculumUnit.subject == subject,
            CurriculumUnit.grade_level == grade_level,
            CurriculumSubUnit.is_active.is_(True),
        )
    )
    if max_unit is not None:
        query = query.filter(CurriculumUnit.unit_number <= max_unit)

    sub_units = query.order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence).all()

    return [
        slot_status(db, sub_unit, difficulty, target)
        for sub_unit in sub_units
        for difficulty in DifficultyLevel
    ]


# --------------------------------------------------------------------------- #
# Filling
# --------------------------------------------------------------------------- #


def _store(
    db: Session,
    sub_unit_id: str,
    difficulty: DifficultyLevel,
    question: GeneratedQuestion,
    metadata: dict,
    status: str,
    verifier_answer: str | None = None,
    votes: dict | None = None,
    notes: str | None = None,
) -> bool:
    """Insert one bank item, skipping duplicates. Returns True if stored."""
    digest = content_hash(question.question_text)
    exists = (
        db.query(QuestionBankItem.id)
        .filter_by(sub_unit_id=sub_unit_id, difficulty_level=difficulty, content_hash=digest)
        .first()
    )
    if exists is not None:
        return False

    item = QuestionBankItem(
        sub_unit_id=sub_unit_id,
        difficulty_level=difficulty,
        question_text=question.question_text,
        question_type=question.question_type,
        options=question.options,
        correct_answer=question.correct_answer,
        explanation=question.explanation,
        distractor_rationales=question.distractor_rationales,
        hint=question.hint or None,
        skill_tag=question.skill_tag or None,
        points=question.points,
        source_chunk_ids=question.source_chunk_ids,
        content_hash=digest,
        generation_metadata=metadata,
        verification_status=status,
        verifier_answer=verifier_answer,
        verification_votes=votes or {},
        review_notes=notes,
        # Disputed items are stored but never served.
        is_active=status != "disputed",
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError:
        # Another process won the race on this content hash. Roll back the
        # uncommitted part of this batch; the caller commits per batch, so the
        # loss is bounded to one batch and this is rare.
        #
        # A SAVEPOINT would scope the rollback more tightly, but SQLAlchemy's
        # begin_nested() runs in autocommit on SQLite (a pysqlite transaction
        # quirk), which would commit every insert immediately and defeat the
        # batching entirely.
        db.rollback()
        return False
    return True


def fill_slot(
    db: Session,
    sub_unit: CurriculumSubUnit,
    difficulty: DifficultyLevel,
    target: int = DEFAULT_BANK_DEPTH,
    agent: QuizGeneratorAgent | None = None,
    max_batches: int = 4,
    report: FillReport | None = None,
) -> FillReport:
    """Top one slot up towards ``target`` servable questions.

    Generates with verification on, storing disputed questions inactive so a
    human can review them rather than losing the signal entirely.

    Commits between batches, and before each one. That is not tidiness: SQLite
    allows a single writer and holds the lock until commit, so a transaction
    left open across a model call blocks every other write in the app for as
    long as the model takes.
    """
    report = report or FillReport()
    report.slots_examined += 1

    status = slot_status(db, sub_unit, difficulty, target)
    if status.is_stocked:
        return report

    # Bank generation is a background job, so verification is always worth it.
    agent = agent or QuizGeneratorAgent(verify=True, verbose=False)
    added_here = 0

    for _ in range(max_batches):
        needed = target - (status.servable + added_here)
        if needed <= 0:
            break

        # Nothing may hold the write lock across `generate()`. SQLite has one
        # writer, the lock is taken at the first write and released only on
        # commit, and generation with verification runs for the better part of
        # a minute -- long enough to push every other writer past
        # `busy_timeout`. Committing here closes any transaction the previous
        # batch left open before the slow part starts.
        db.commit()

        try:
            generated = agent.generate(
                sub_unit=sub_unit.id,
                difficulty=difficulty,
                count=min(needed, 10),
                db=db,
            )
        except Exception as exc:
            message = f"{sub_unit.sub_unit_number}/{difficulty.value}: {exc}"
            logger.error("Bank fill failed for %s", message)
            report.errors.append(message)
            break

        metadata = generated.generation_metadata()

        for question in generated.questions:
            if _store(db, sub_unit.id, difficulty, question, metadata, "verified"):
                added_here += 1
                report.questions_added += 1
            else:
                report.duplicates_skipped += 1

        # Keep what the verifier rejected, inactive, so it can be reviewed
        # rather than silently discarded.
        for verdict in generated.rejected:
            placeholder = GeneratedQuestion(
                question_number=0,
                question_text=verdict.question_text,
                options=[],
                correct_answer=verdict.claimed_answer,
                explanation="(quarantined before review)",
                distractor_rationales={},
            )
            stored = _store(
                db,
                sub_unit.id,
                difficulty,
                placeholder,
                metadata,
                "disputed",
                verifier_answer=verdict.verifier_answer,
                votes=verdict.votes,
                notes="; ".join(verdict.issues) or "verifier disputed the answer key",
            )
            if stored:
                report.quarantined += 1

        # Commit before generating the next batch. SQLite allows one writer at
        # a time, and the lock is taken at the first INSERT and held until
        # commit -- so leaving the transaction open across the next
        # ``agent.generate()`` call would hold the write lock for minutes and
        # make every concurrent filler fail with "database is locked".
        db.commit()

    if added_here:
        report.slots_filled += 1
        logger.info(
            "Bank: %s/%s +%d question(s).",
            sub_unit.sub_unit_number,
            difficulty.value,
            added_here,
        )

    return report


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


def seen_bank_ids(
    db: Session, student_id: str, sub_unit_id: str, difficulty: DifficultyLevel
) -> set[str]:
    """Bank items this student has already been served for a slot.

    Excluding these is what makes a retry feel like a genuinely new quiz
    rather than the same ten questions in a different order.
    """
    rows = db.execute(
        select(QuizQuestion.bank_question_id)
        .join(Quiz, Quiz.id == QuizQuestion.quiz_id)
        .where(
            Quiz.student_id == student_id,
            Quiz.sub_unit_id == sub_unit_id,
            Quiz.difficulty_level == difficulty,
            QuizQuestion.bank_question_id.is_not(None),
        )
    ).all()
    return {row[0] for row in rows if row[0]}


def draw_questions(
    db: Session,
    student_id: str,
    sub_unit_id: str,
    difficulty: DifficultyLevel,
    count: int,
    rng: random.Random | None = None,
) -> tuple[list[QuestionBankItem], bool]:
    """Draw ``count`` questions for a student, preferring unseen ones.

    Returns:
        ``(items, all_fresh)`` where ``all_fresh`` is True when every drawn
        question is new to this student. When the bank cannot supply enough
        unseen items the remainder is topped up with the least-recently-served
        seen ones -- a repeated question beats no quiz at all.
    """
    rng = rng or random.Random()

    available = (
        db.query(QuestionBankItem)
        .filter_by(sub_unit_id=sub_unit_id, difficulty_level=difficulty, is_active=True)
        .filter(QuestionBankItem.verification_status != "disputed")
        .all()
    )
    if not available:
        return [], True

    seen = seen_bank_ids(db, student_id, sub_unit_id, difficulty)
    unseen = [item for item in available if item.id not in seen]
    rng.shuffle(unseen)

    drawn = unseen[:count]
    all_fresh = len(drawn) == count

    if not all_fresh:
        repeats = [item for item in available if item.id in seen]
        # Oldest-served first, so a repeat is at least the least familiar one.
        repeats.sort(key=lambda item: (item.last_served_at or datetime.min.replace(tzinfo=UTC)))
        drawn.extend(repeats[: count - len(drawn)])

    return drawn, all_fresh


def mark_served(db: Session, items: list[QuestionBankItem]) -> None:
    """Record that these bank items were handed to a student."""
    now = datetime.now(UTC)
    for item in items:
        item.times_served = (item.times_served or 0) + 1
        item.last_served_at = now


def retire(db: Session, bank_question_id: str, reason: str) -> QuestionBankItem | None:
    """Deactivate a bank item so it is never served again.

    Soft, because student answer history points at this row.
    """
    item = db.get(QuestionBankItem, bank_question_id)
    if item is None:
        return None
    item.is_active = False
    item.review_notes = reason
    logger.warning("Retired bank question %s: %s", bank_question_id, reason)
    return item


def disputed_items(db: Session, limit: int = 50) -> list[QuestionBankItem]:
    """Quarantined questions awaiting human review."""
    return (
        db.query(QuestionBankItem)
        .filter_by(verification_status="disputed")
        .order_by(QuestionBankItem.created_at.desc())
        .limit(limit)
        .all()
    )


def total_servable(db: Session) -> int:
    """Count every question currently servable across the whole bank."""
    return (
        db.execute(
            select(func.count(QuestionBankItem.id)).where(
                QuestionBankItem.is_active.is_(True),
                QuestionBankItem.verification_status != "disputed",
            )
        ).scalar_one()
        or 0
    )


__all__ = [
    "DEFAULT_BANK_DEPTH",
    "FillReport",
    "SlotStatus",
    "bank_overview",
    "content_hash",
    "disputed_items",
    "draw_questions",
    "fill_slot",
    "mark_served",
    "retire",
    "seen_bank_ids",
    "slot_status",
    "total_servable",
]
