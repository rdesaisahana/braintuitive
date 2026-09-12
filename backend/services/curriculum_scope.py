"""Which curriculum a given student learns from.

Curriculum is owned: a parent uploads their child's guide and it belongs to
them. That is one rule, and it is answered here rather than at each of the five
places that query units -- five hand-written copies of a visibility rule drift,
and the direction they drift is one family seeing another family's material.

The rule: **a child sees their own family's curriculum, and nothing else.**

There is deliberately no fallback. An earlier version showed families a shared
sample curriculum until they uploaded, so a new account was never an empty
screen -- but the questions in it came from one district's Pre-Algebra guide,
and serving those to a child whose parent never chose them is a quiet lie about
what the child is being taught. A parent uploads first; the app says so plainly
rather than filling the gap with someone else's syllabus.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import CurriculumUnit, Student


def curriculum_owner(db: Session, student: Student) -> str | None:
    """Whose curriculum this student works from.

    Always their own parent. Kept as a function rather than inlined because
    every caller needs the same answer, and because it used to be able to
    return None for the shared sample -- callers that still handle None are
    handling a case that can no longer happen, not a live one.
    """
    return student.user_id


def visible_units(db: Session, student: Student) -> list[CurriculumUnit]:
    """Every unit this student can see, in order.

    Empty until their parent uploads a curriculum. That empty list is a real
    answer the app is expected to render -- "upload your school's guide to get
    started" -- not a failure to fall back from.
    """
    return (
        db.query(CurriculumUnit)
        .filter_by(
            user_id=curriculum_owner(db, student),
            grade_level=student.grade_level,
            is_active=True,
        )
        .order_by(CurriculumUnit.unit_number)
        .all()
    )


def has_own_curriculum(db: Session, user_id: str) -> bool:
    """Whether this parent has uploaded any curriculum at all.

    Grade-agnostic on purpose: it answers "should we still be asking them to
    upload?", and a parent who has uploaded a grade-7 guide has clearly found
    the upload screen.
    """
    return (
        db.query(CurriculumUnit.id).filter_by(user_id=user_id, is_active=True).first() is not None
    )


__all__ = ["curriculum_owner", "has_own_curriculum", "visible_units"]
