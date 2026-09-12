"""Progress routes: what a parent opens.

Every other router serves the child mid-task. This one answers a parent's
questions: is my kid actually learning, where are they stuck, and are they
turning up?

The most useful answer is not an overall percentage -- "72%" tells a parent
nothing they can act on. It is the per-skill breakdown: *subtract_integers,
4 of 11 correct, 3 hints used*. That is a conversation they can have at the
kitchen table, and it is the same signal the Gap Detector agent will consume.

Practice attempts are excluded from statistics by default. A retry after
mastery is deliberately low-stakes -- children click through them -- so
folding those scores into "average" or "accuracy" would understate what the
child actually knows. They remain visible in history, labelled, and
``include_practice=true`` brings them back into the numbers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Integer, func, or_
from sqlalchemy.orm import Session

from agents.gap_detector import (
    MIN_EVIDENCE,
    WEAK_ACCURACY,
    Gap,
    GapDetectorAgent,
    GapReport,
)
from agents.test_prep import StudyPlan, TestPrepAgent
from api.deps import get_owned_student
from api.schemas import (
    AttemptOut,
    DrillBlockOut,
    GapOut,
    GapReportOut,
    OptionOut,
    ProgressSummaryOut,
    ReviewQuestionOut,
    RevisionPointOut,
    SkillStatOut,
    StudyPlanOut,
    StudyPlanRequest,
    StudySessionOut,
    SubUnitDetailOut,
    SubUnitProgressOut,
    TopicReviewOut,
    TopicRiskOut,
)
from db.database import get_db
from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    DifficultyLevel,
    ProgressStatus,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizResponse,
    Student,
    SubUnitProgress,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/progress", tags=["progress"])

# A skill is flagged once there is enough evidence to be worth acting on.
# Fewer than this and one unlucky question would light up the dashboard.
# Shared with the Gap Detector deliberately: a dashboard that flags a skill the
# gap report then calls healthy (or the reverse) is worse than either alone.
MIN_ANSWERS_FOR_ATTENTION = MIN_EVIDENCE
ATTENTION_ACCURACY = WEAK_ACCURACY


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _streak_days(attempt_dates: set[date], today: date | None = None) -> int:
    """Consecutive days of activity ending today or yesterday.

    Yesterday still counts: a child who worked last night and has not started
    today has not broken their streak, and telling them they have at 9am is
    both wrong and discouraging.
    """
    if not attempt_dates:
        return 0

    today = today or datetime.now(UTC).date()
    if today in attempt_dates:
        cursor = today
    elif (today - timedelta(days=1)) in attempt_dates:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor in attempt_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _skill_stats(
    db: Session,
    student_id: str,
    sub_unit_id: str | None = None,
    include_practice: bool = False,
) -> list[SkillStatOut]:
    """Aggregate accuracy and hint reliance per skill tag.

    Built from individual responses rather than the ``skill_breakdown`` JSON on
    ``quiz_attempts``: responses carry ``hint_used``, and "got it right but
    needed the hint" is a materially different signal from "got it right".
    """
    query = (
        db.query(
            QuizQuestion.skill_tag,
            func.count(QuizResponse.id),
            func.sum(func.cast(QuizResponse.is_correct, Integer)),
            func.sum(func.cast(QuizResponse.hint_used, Integer)),
        )
        .join(QuizQuestion, QuizQuestion.id == QuizResponse.question_id)
        .join(Quiz, Quiz.id == QuizResponse.quiz_id)
        .filter(QuizResponse.student_id == student_id)
        .group_by(QuizQuestion.skill_tag)
    )
    if not include_practice:
        query = query.filter(Quiz.is_practice.is_(False))
    if sub_unit_id is not None:
        query = query.filter(Quiz.sub_unit_id == sub_unit_id)

    stats: list[SkillStatOut] = []
    for tag, answered, correct, hints in query.all():
        answered = int(answered or 0)
        correct = int(correct or 0)
        hints = int(hints or 0)
        if not answered:
            continue
        accuracy = correct / answered
        stats.append(
            SkillStatOut(
                skill_tag=tag or "untagged",
                questions_answered=answered,
                correct=correct,
                accuracy=round(accuracy, 3),
                hints_used=hints,
                needs_attention=(
                    answered >= MIN_ANSWERS_FOR_ATTENTION and accuracy < ATTENTION_ACCURACY
                ),
            )
        )

    # Weakest first: the dashboard should lead with what needs work.
    stats.sort(key=lambda stat: (stat.accuracy, -stat.questions_answered))
    return stats


def _progress_view(
    sub_unit: CurriculumSubUnit, progress: SubUnitProgress | None
) -> SubUnitProgressOut:
    """One sub-unit's progress row, defaulting cleanly when never started."""
    return SubUnitProgressOut(
        sub_unit_id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        title=sub_unit.title,
        completion_percentage=progress.completion_percentage if progress else 0,
        status=progress.status if progress else ProgressStatus.NOT_STARTED,
        beginner_best_score=progress.beginner_best_score if progress else 0.0,
        intermediate_best_score=progress.intermediate_best_score if progress else 0.0,
        proficient_best_score=progress.proficient_best_score if progress else 0.0,
        total_attempts=progress.total_attempts if progress else 0,
    )


def _attempt_view(attempt: QuizAttempt, sub_unit: CurriculumSubUnit | None) -> AttemptOut:
    return AttemptOut(
        id=attempt.id,
        unit_number=sub_unit.unit.unit_number if sub_unit else None,
        sub_unit_id=attempt.sub_unit_id,
        sub_unit_number=sub_unit.sub_unit_number if sub_unit else "?",
        sub_unit_title=sub_unit.title if sub_unit else "(removed)",
        difficulty_level=attempt.difficulty_level,
        attempt_number=attempt.attempt_number,
        score_percentage=attempt.score_percentage,
        correct_count=attempt.correct_count,
        total_questions=attempt.total_questions,
        is_passed=attempt.is_passed,
        is_practice=attempt.is_practice,
        duration_seconds=attempt.duration_seconds,
        completed_at=attempt.completed_at,
    )


LEVEL_NAMES = {
    DifficultyLevel.BEGINNER: "Easy",
    DifficultyLevel.INTERMEDIATE: "Medium",
    DifficultyLevel.PROFICIENT: "Tricky",
}


def _skill_name(tag: str | None) -> str:
    """ "divide_integers" -> "Divide integers"."""
    words = (tag or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Other questions"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _review_question(
    response: QuizResponse, question: QuizQuestion, *, since_correct: bool = False
) -> ReviewQuestionOut:
    """One question for the review, carrying only what the child already saw."""
    wrong = not response.is_correct
    rationales = question.distractor_rationales or {}
    kind = question.question_type
    return ReviewQuestionOut(
        question_id=question.id,
        question_text=question.question_text,
        question_type=getattr(kind, "value", kind) or "multiple_choice",
        options=[OptionOut(**option) for option in (question.options or [])],
        difficulty_level=question.difficulty_level,
        skill_tag=question.skill_tag,
        selected_answer=response.selected_answer,
        is_correct=response.is_correct,
        hint_used=response.hint_used,
        correct_answer=question.correct_answer if wrong else None,
        explanation=question.explanation if wrong else None,
        why_your_answer_was_wrong=(
            rationales.get((response.selected_answer or "").upper()) if wrong else None
        ),
        hint=question.hint if response.hint_used else None,
        since_answered_correctly=since_correct,
        answered_at=response.answered_at,
    )


def _topic_review(db: Session, student: Student, sub_unit: CurriculumSubUnit) -> TopicReviewOut:
    """Everything worth revising in one topic, from every quiz that touched it.

    Easy, Medium and Tricky quizzes all count, and so does practice: a mistake
    made while practising is still a mistake worth another look. So do the
    topic's questions inside a unit test, found by the question's own
    ``sub_unit_id``.

    The same bank question can be served more than once, so answers are grouped
    by the bank item they were copied from. A question counts as missed if it
    was ever answered wrong -- and says so if it has since been answered right --
    and as hinted if a hint was needed to get it right.
    """
    rows = (
        db.query(QuizResponse, QuizQuestion)
        .join(QuizQuestion, QuizQuestion.id == QuizResponse.question_id)
        .join(Quiz, Quiz.id == QuizResponse.quiz_id)
        .filter(QuizResponse.student_id == student.id)
        .filter(or_(Quiz.sub_unit_id == sub_unit.id, QuizQuestion.sub_unit_id == sub_unit.id))
        .order_by(QuizResponse.answered_at.desc())
        .all()
    )

    by_question: dict[str, list[tuple[QuizResponse, QuizQuestion]]] = defaultdict(list)
    for response, question in rows:
        by_question[question.bank_question_id or question.id].append((response, question))

    missed: list[ReviewQuestionOut] = []
    hinted: list[ReviewQuestionOut] = []
    for answers in by_question.values():  # each list is newest first
        latest_response, _ = answers[0]
        wrong = [(r, q) for r, q in answers if not r.is_correct]
        if wrong:
            response, question = wrong[0]
            since_correct = latest_response.is_correct and latest_response is not response
            missed.append(_review_question(response, question, since_correct=since_correct))
            continue
        with_hint = [(r, q) for r, q in answers if r.hint_used]
        if with_hint:
            hinted.append(_review_question(*with_hint[0]))

    # Per skill, over every answer: how often wrong, how often a hint was needed.
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # answered, wrong, hints
    wrong_by_level: dict[DifficultyLevel, int] = defaultdict(int)
    hints_by_level: dict[DifficultyLevel, int] = defaultdict(int)
    for response, question in rows:
        tally = counts[question.skill_tag or ""]
        tally[0] += 1
        if not response.is_correct:
            tally[1] += 1
            wrong_by_level[question.difficulty_level] += 1
        if response.hint_used:
            tally[2] += 1
            hints_by_level[question.difficulty_level] += 1

    revise: list[RevisionPointOut] = []
    for tag, (answered, wrong_count, hints) in counts.items():
        if not wrong_count and not hints:
            continue
        name = _skill_name(tag)
        if wrong_count:
            message = f"{name}: {wrong_count} of {answered} wrong"
            if hints:
                message += f", and {_plural(hints, 'hint')} needed"
        else:
            message = f"{name}: all right, but {_plural(hints, 'hint')} needed"
        revise.append(
            RevisionPointOut(
                skill_tag=tag or "untagged",
                skill_name=name,
                answered=answered,
                wrong=wrong_count,
                hints_used=hints,
                message=message,
            )
        )
    # Most wrong first, then the ones leaning hardest on hints.
    revise.sort(key=lambda point: (-point.wrong / point.answered, -point.hints_used, point.skill_name))

    level_counts = wrong_by_level or hints_by_level
    order = list(DifficultyLevel)
    revise_level = (
        max(level_counts, key=lambda level: (level_counts[level], -order.index(level)))
        if level_counts
        else None
    )

    unit = sub_unit.unit
    topic = f"{sub_unit.sub_unit_number} {sub_unit.title}"
    if not rows:
        summary = f"Nothing answered in {topic} yet, so there is nothing to revise."
    elif not missed and not hinted:
        summary = "Nothing to revise: every answer was right, and without a hint."
    else:
        parts = []
        if missed:
            parts.append(f"{_plural(len(missed), 'question')} to look at again")
        if hinted:
            parts.append(f"{len(hinted)} that needed a hint")
        summary = " and ".join(parts).capitalize() + "."
        if revise:
            summary += f" Revise {revise[0].skill_name.lower()} first."
        if revise_level is not None:
            where = f"Unit {unit.unit_number}, {topic}" if unit else topic
            summary += f" Go back to {where} at {LEVEL_NAMES[revise_level]}."

    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student.id, sub_unit_id=sub_unit.id)
        .one_or_none()
    )
    newest_first = lambda item: item.answered_at  # noqa: E731
    return TopicReviewOut(
        sub_unit_id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        title=sub_unit.title,
        unit_number=unit.unit_number if unit else None,
        unit_title=unit.title if unit else None,
        completion_percentage=progress.completion_percentage if progress else 0,
        questions_answered=len(rows),
        summary=summary,
        revise_level=revise_level,
        revise=revise,
        missed=sorted(missed, key=newest_first, reverse=True),
        hinted=sorted(hinted, key=newest_first, reverse=True),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/students/{student_id}", response_model=ProgressSummaryOut)
def read_summary(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
    include_practice: bool = Query(
        default=False, description="Fold practice retries into the statistics"
    ),
) -> ProgressSummaryOut:
    """The parent dashboard headline for one student."""
    sub_units: list[CurriculumSubUnit] = (
        db.query(CurriculumSubUnit)
        .join(CurriculumUnit)
        .filter(
            CurriculumUnit.grade_level == student.grade_level,
            CurriculumUnit.is_active.is_(True),
            CurriculumSubUnit.is_active.is_(True),
        )
        .order_by(CurriculumUnit.unit_number, CurriculumSubUnit.sequence)
        .all()
    )
    sub_unit_ids = [sub_unit.id for sub_unit in sub_units]

    progress_rows: list[SubUnitProgress] = (
        db.query(SubUnitProgress)
        .filter(
            SubUnitProgress.student_id == student.id,
            SubUnitProgress.sub_unit_id.in_(sub_unit_ids),
        )
        .all()
        if sub_unit_ids
        else []
    )
    progress_by_id = {row.sub_unit_id: row for row in progress_rows}

    attempts: list[QuizAttempt] = db.query(QuizAttempt).filter_by(student_id=student.id).all()
    graded = [a for a in attempts if not a.is_practice]
    counted = attempts if include_practice else graded

    completed_ids = {row.sub_unit_id for row in progress_rows if row.completion_percentage == 100}

    # Units completed: every sub-unit in the unit is at 100%.
    by_unit: dict[int, list[CurriculumSubUnit]] = defaultdict(list)
    for sub_unit in sub_units:
        by_unit[sub_unit.unit.unit_number].append(sub_unit)
    units_completed = sum(
        1
        for members in by_unit.values()
        if members and all(member.id in completed_ids for member in members)
    )

    # Current unit: the earliest that is not finished.
    current_unit = next(
        (
            number
            for number in sorted(by_unit)
            if not all(member.id in completed_ids for member in by_unit[number])
        ),
        None,
    )

    overall = (
        sum(row.completion_percentage for row in progress_rows) / len(sub_units)
        if sub_units
        else 0.0
    )
    average = sum(a.score_percentage for a in counted) / len(counted) if counted else 0.0
    last_active = max((a.completed_at for a in attempts), default=None)

    views = [_progress_view(sub_unit, progress_by_id.get(sub_unit.id)) for sub_unit in sub_units]

    return ProgressSummaryOut(
        student_id=student.id,
        student_name=student.display_name,
        grade_level=student.grade_level,
        current_unit=current_unit,
        units_completed=units_completed,
        sub_units_completed=len(completed_ids),
        sub_units_total=len(sub_units),
        overall_percentage=round(overall, 1),
        quizzes_completed=len(graded),
        practice_quizzes=len(attempts) - len(graded),
        average_score=round(average, 1),
        total_time_seconds=sum(row.total_time_seconds for row in progress_rows),
        current_streak_days=_streak_days(
            {a.completed_at.date() for a in attempts if a.completed_at}
        ),
        last_active=last_active,
        celebrations_pending=sum(1 for row in progress_rows if row.should_celebrate),
        sub_units=views,
    )


@router.get("/students/{student_id}/skills", response_model=list[SkillStatOut])
def read_skills(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
    include_practice: bool = Query(default=False),
) -> list[SkillStatOut]:
    """Per-skill accuracy and hint reliance, weakest first.

    The actionable view: not "72% overall" but which specific skill is shaky.
    """
    return _skill_stats(db, student.id, include_practice=include_practice)


def _gap_view(gap: Gap) -> GapOut:
    return GapOut(
        skill_tag=gap.skill_tag,
        sub_unit_id=gap.sub_unit_id,
        sub_unit_number=gap.sub_unit_number,
        sub_unit_title=gap.sub_unit_title,
        unit_number=gap.unit_number,
        severity=gap.severity,
        accuracy=gap.accuracy,
        questions_answered=gap.questions_answered,
        hints_used=gap.hints_used,
        evidence=gap.evidence,
        likely_misconception=gap.likely_misconception,
        recommendation=gap.recommendation,
        recommended_difficulty=gap.recommended_difficulty,
    )


def _gap_report_view(report: GapReport) -> GapReportOut:
    return GapReportOut(
        student_id=report.student_id,
        student_name=report.student_name,
        summary=report.summary,
        gaps=[_gap_view(gap) for gap in report.gaps],
        strengths=report.strengths,
        skills_analysed=report.skills_analysed,
        responses_analysed=report.responses_analysed,
        skills_with_thin_evidence=report.skills_with_thin_evidence,
    )


@router.post("/students/{student_id}/gaps", response_model=GapReportOut)
def analyse_gaps(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> GapReportOut:
    """Diagnose why a student is struggling, not merely that they are.

    POST rather than GET because this costs a model call and is not cacheable:
    it is a parent pressing "explain this", not a page load.

    The evidence behind every finding is gathered in SQL and returned in
    ``evidence``, so a parent can check the diagnosis against the actual wrong
    answers rather than taking the model's word for it. If the model is
    unavailable the report still comes back, carrying the counted
    misconceptions without the interpretation.
    """
    try:
        report = GapDetectorAgent().analyse(student.id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Gap analysis failed for student %s", student.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gap analysis is unavailable right now. Please try again shortly.",
        ) from exc

    return _gap_report_view(report)


def _study_plan_view(plan: StudyPlan) -> StudyPlanOut:
    return StudyPlanOut(
        student_id=plan.student_id,
        student_name=plan.student_name,
        unit_numbers=plan.unit_numbers,
        days_until_test=plan.days_until_test,
        total_questions=plan.total_questions,
        summary=plan.summary,
        advice=plan.advice,
        sessions=[
            StudySessionOut(
                day=session.day,
                focus=session.focus,
                question_count=session.question_count,
                blocks=[DrillBlockOut(**block.to_dict()) for block in session.blocks],
            )
            for session in plan.sessions
        ],
        risks=[TopicRiskOut(**risk.to_dict()) for risk in plan.risks],
        topics_not_yet_started=plan.topics_not_yet_started,
        warnings=plan.warnings,
        rehearsal_unit_ids=plan.rehearsal_unit_ids,
    )


@router.post("/students/{student_id}/study-plan", response_model=StudyPlanOut)
def build_study_plan(
    body: StudyPlanRequest,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> StudyPlanOut:
    """Plan revision for an upcoming test.

    Answers a question nothing else here does: given the evenings available,
    what should this child revise and in what order? The ranking combines how
    much of each topic is unfinished, how weak the child is at its skills, and
    -- the signal the dashboard structurally cannot show -- how long ago they
    last touched it. A tier passed in March reads as ``completed`` exactly like
    one passed yesterday.

    Planning is analysis: it awards nothing and moves no progress. Each block
    names the sub-unit and tier to drill, so the child starts them through the
    ordinary quiz endpoints, as practice.
    """
    try:
        plan = TestPrepAgent().plan(
            student.id,
            unit_numbers=body.unit_numbers,
            days_until_test=body.days_until_test,
            question_budget=body.question_budget,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Study planning failed for student %s", student.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Study planning is unavailable right now. Please try again shortly.",
        ) from exc

    return _study_plan_view(plan)


@router.get("/students/{student_id}/attempts", response_model=list[AttemptOut])
def read_attempts(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_practice: bool = Query(default=True, description="Practice shows in history"),
) -> list[AttemptOut]:
    """Attempt history, newest first.

    Practice attempts are included here by default -- a parent should see the
    work, even where it does not count towards a score.
    """
    query = db.query(QuizAttempt).filter_by(student_id=student.id)
    if not include_practice:
        query = query.filter(QuizAttempt.is_practice.is_(False))

    attempts = query.order_by(QuizAttempt.completed_at.desc()).limit(limit).offset(offset).all()
    if not attempts:
        return []

    sub_units = {
        sub_unit.id: sub_unit
        for sub_unit in db.query(CurriculumSubUnit)
        .filter(CurriculumSubUnit.id.in_({a.sub_unit_id for a in attempts}))
        .all()
    }
    return [_attempt_view(attempt, sub_units.get(attempt.sub_unit_id)) for attempt in attempts]


@router.get("/students/{student_id}/sub-units/{sub_unit_id}/review", response_model=TopicReviewOut)
def read_topic_review(
    sub_unit_id: str,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> TopicReviewOut:
    """What to revise in one topic: wrong answers, hinted answers, and advice.

    Shown when a topic is completed, and available any time from the progress
    history. Only this child's own answers are read, so another family's
    topic simply reviews as empty.
    """
    sub_unit = db.get(CurriculumSubUnit, sub_unit_id)
    if sub_unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-unit not found.")
    return _topic_review(db, student, sub_unit)


@router.get("/students/{student_id}/sub-units/{sub_unit_id}", response_model=SubUnitDetailOut)
def read_sub_unit_detail(
    sub_unit_id: str,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> SubUnitDetailOut:
    """One sub-unit in depth: best scores, every attempt, per-skill accuracy."""
    sub_unit = db.get(CurriculumSubUnit, sub_unit_id)
    if sub_unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-unit not found.")

    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=student.id, sub_unit_id=sub_unit_id)
        .one_or_none()
    )
    attempts = (
        db.query(QuizAttempt)
        .filter_by(student_id=student.id, sub_unit_id=sub_unit_id)
        .order_by(QuizAttempt.completed_at.desc())
        .all()
    )

    return SubUnitDetailOut(
        sub_unit_id=sub_unit.id,
        sub_unit_number=sub_unit.sub_unit_number,
        title=sub_unit.title,
        unit_number=sub_unit.unit.unit_number,
        completion_percentage=progress.completion_percentage if progress else 0,
        status=progress.status if progress else ProgressStatus.NOT_STARTED,
        beginner_best_score=progress.beginner_best_score if progress else 0.0,
        intermediate_best_score=progress.intermediate_best_score if progress else 0.0,
        proficient_best_score=progress.proficient_best_score if progress else 0.0,
        next_difficulty=progress.next_difficulty if progress else None,
        total_attempts=progress.total_attempts if progress else 0,
        total_time_seconds=progress.total_time_seconds if progress else 0,
        attempts=[_attempt_view(attempt, sub_unit) for attempt in attempts],
        skills=_skill_stats(db, student.id, sub_unit_id=sub_unit_id, include_practice=True),
    )
