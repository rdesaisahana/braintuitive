"""Removing a family's curriculum.

This is the most destructive thing a parent can do in the app, and it is not
obvious from the outside why. Curriculum is the root of everything: units own
sub-units, and sub-units own the quizzes taken against them, the progress
earned on them, and the pre-generated question bank. Deleting the curriculum
takes all of it.

So the work here is mostly in *saying so*. :func:`preview` counts what would go
without touching anything, so the interface can tell a parent "this also
removes 240 answered questions and Aanya's progress on 6 topics" rather than
asking them to confirm an abstraction. :func:`delete_curriculum` then does it
and reports what it actually removed.

Points and badges deliberately survive. They live on the gamification profile,
which hangs off the student rather than the curriculum, and a child who earned
2,000 points should not lose the character they bought because a parent
replaced a PDF.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    CurriculumUpload,
    QuestionBankItem,
    Quiz,
    SubUnitProgress,
)
from rag.pinecone_client import PineconeStore, namespace_for

logger = logging.getLogger(__name__)


@dataclass
class DeletionReport:
    """What removing a curriculum would take, or did take."""

    units: int = 0
    sub_units: int = 0
    quizzes: int = 0
    progress_rows: int = 0
    bank_questions: int = 0
    uploads: int = 0
    vectors_dropped: bool = False

    @property
    def is_empty(self) -> bool:
        return self.units == 0 and self.uploads == 0

    def summary(self) -> str:
        return (
            f"{self.units} unit(s), {self.sub_units} topic(s), {self.quizzes} quiz(zes), "
            f"{self.progress_rows} progress row(s), {self.bank_questions} banked question(s), "
            f"{self.uploads} upload record(s)"
        )


def _sub_unit_ids(db: Session, user_id: str) -> list[str]:
    return [
        row[0]
        for row in db.query(CurriculumSubUnit.id)
        .join(CurriculumUnit)
        .filter(CurriculumUnit.user_id == user_id)
        .all()
    ]


def preview(db: Session, user_id: str) -> DeletionReport:
    """Count what deleting this family's curriculum would remove.

    Reads only. The point is to let a parent see the cost before agreeing to
    it -- "are you sure?" against an unnamed quantity is not consent.
    """
    report = DeletionReport()
    sub_unit_ids = _sub_unit_ids(db, user_id)

    report.units = (
        db.query(func.count(CurriculumUnit.id)).filter(CurriculumUnit.user_id == user_id).scalar()
        or 0
    )
    report.sub_units = len(sub_unit_ids)
    report.uploads = (
        db.query(func.count(CurriculumUpload.id))
        .filter(CurriculumUpload.user_id == user_id)
        .scalar()
        or 0
    )

    if sub_unit_ids:
        report.quizzes = (
            db.query(func.count(Quiz.id)).filter(Quiz.sub_unit_id.in_(sub_unit_ids)).scalar() or 0
        )
        report.progress_rows = (
            db.query(func.count(SubUnitProgress.id))
            .filter(SubUnitProgress.sub_unit_id.in_(sub_unit_ids))
            .scalar()
            or 0
        )
        report.bank_questions = (
            db.query(func.count(QuestionBankItem.id))
            .filter(QuestionBankItem.sub_unit_id.in_(sub_unit_ids))
            .scalar()
            or 0
        )

    return report


def delete_curriculum(db: Session, user_id: str, drop_vectors: bool = True) -> DeletionReport:
    """Remove a family's curriculum and everything that hangs off it.

    Args:
        db: Active session. The caller commits.
        user_id: Whose curriculum to remove.
        drop_vectors: Also delete their Pinecone namespace. Left switchable so
            tests can run without a live index.

    Returns:
        A :class:`DeletionReport` of what was removed.
    """
    report = preview(db, user_id)
    if report.is_empty:
        logger.info("Nothing to delete: %s owns no curriculum.", user_id[:8])
        return report

    units = db.query(CurriculumUnit).filter(CurriculumUnit.user_id == user_id).all()
    grades = {unit.grade_level for unit in units}
    subjects = {unit.subject for unit in units}

    # Deleting the units cascades to sub-units, and from there to quizzes,
    # progress and the bank. Done through the ORM rather than a bulk DELETE so
    # those cascades actually run.
    for unit in units:
        db.delete(unit)

    for upload in db.query(CurriculumUpload).filter(CurriculumUpload.user_id == user_id).all():
        db.delete(upload)

    db.flush()

    if drop_vectors:
        # Their vectors are in a namespace of their own, so this is a drop
        # rather than a filtered scan. A failure here costs storage, not
        # correctness: the namespace is keyed by user id, so nobody else can
        # ever read it, and a re-upload replaces it.
        try:
            store = PineconeStore()
            for subject in subjects or {"math"}:
                for grade in grades:
                    store.delete_namespace(namespace_for(subject, grade, user_id))
            report.vectors_dropped = True
        except Exception:
            logger.exception(
                "Could not drop the vector namespace for %s; it is orphaned but "
                "unreachable, and a re-upload will replace it.",
                user_id[:8],
            )

    logger.info("Deleted curriculum for %s: %s", user_id[:8], report.summary())
    return report


__all__ = ["DeletionReport", "delete_curriculum", "preview"]
