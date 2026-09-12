"""Quiz routes: start a quiz, answer a question, finish.

The shape of this flow is the product:

    POST /quiz/start                        -> ten questions, WITHOUT answers
    POST /quiz/{id}/questions/{qid}/hint    -> the hint, only when asked for
    POST /quiz/{id}/answer                  -> feedback, one question at a time
    POST /quiz/{id}/complete                -> score, progress, celebration

Answering is per-question rather than a single submit at the end. That is the
Khan Academy pattern and the reason feedback lands instantly: the explanation
and the per-option rationale were generated and verified when the question was
banked, so answering costs a database read, not a model call.

A quiz is resumable. ``POST /quiz/start`` hands back an unfinished quiz
rather than creating a second one, so a child who closes the tab mid-quiz
returns to exactly where they were.

Two deliberate omissions:

* **A correct answer gets no explanation.** Explaining what the student just
  demonstrated they understand is noise, and it buries the feedback that
  matters when they are wrong.
* **The hint is not shipped with the question.** It is fetched from its own
  endpoint, which is also what makes ``hint_used`` trustworthy -- the server
  records that the hint was served rather than believing the client.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.deps import assert_owns_student, get_current_user
from api.schemas import (
    AnsweredState,
    AnswerFeedback,
    AnswerRequest,
    AwardOut,
    BadgeOut,
    HintOut,
    OptionOut,
    QuestionForStudent,
    QuizOut,
    QuizResultOut,
    QuizStartRequest,
    SubUnitScoreOut,
    UnitTestResultOut,
    UnitTestStartRequest,
)
from db.database import get_db
from db.models import (
    CurriculumSubUnit,
    CurriculumUnit,
    Quiz,
    QuestionType,
    QuizQuestion,
    QuizResponse,
    QuizStatus,
    SubUnitProgress,
    User,
)
from services.gamification import award_for_attempt
from services.quiz_service import (
    QuizServiceError,
    UnitLockedError,
    create_quiz,
    find_resumable_quiz,
    record_attempt,
)
from services.unit_test import (
    create_unit_test,
    find_resumable_unit_test,
    sub_unit_breakdown,
    sub_unit_titles,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quiz", tags=["quiz"])


def _to_student_view(
    db: Session,
    quiz: Quiz,
    sub_unit: CurriculumSubUnit | None,
    resumed: bool = False,
) -> QuizOut:
    """Serialise a quiz for the student.

    Unanswered questions carry no answer fields at all. Answered ones carry
    what the student was already shown -- their choice, whether it was right,
    and the feedback -- so a quiz picked up the next day looks exactly as they
    left it rather than as a blank slate.
    """
    responses = {
        response.question_id: response
        for response in db.query(QuizResponse).filter_by(quiz_id=quiz.id).all()
    }

    questions: list[QuestionForStudent] = []
    for question in sorted(quiz.questions, key=lambda q: q.question_number):
        response = responses.get(question.id)
        view = QuestionForStudent(
            id=question.id,
            question_number=question.question_number,
            question_text=question.question_text,
            question_type=QuestionType(question.question_type or QuestionType.MULTIPLE_CHOICE).value,
            options=[OptionOut(**option) for option in (question.options or [])],
            has_hint=bool(question.hint),
            points=question.points,
        )
        if response is not None:
            rationales = question.distractor_rationales or {}
            wrong = not response.is_correct
            view.answered = AnsweredState(
                selected_answer=response.selected_answer,
                is_correct=response.is_correct,
                hint_used=response.hint_used,
                # Only for a wrong answer, matching the live feedback rule.
                correct_answer=question.correct_answer if wrong else None,
                explanation=question.explanation if wrong else None,
                why_your_answer_was_wrong=(
                    rationales.get((response.selected_answer or "").upper()) if wrong else None
                ),
            )
        questions.append(view)

    answered = sum(1 for view in questions if view.answered)
    next_number = next((view.question_number for view in questions if not view.answered), None)

    # A unit test names no sub-unit, so its heading comes from the unit. Left
    # to the sub-unit fallback it would render as "?" -- a child sitting a
    # cumulative paper would be told they are working on nothing in particular.
    unit = quiz.unit if quiz.is_unit_test else (sub_unit.unit if sub_unit else None)

    return QuizOut(
        id=quiz.id,
        student_id=quiz.student_id,
        sub_unit_id=quiz.sub_unit_id,
        sub_unit_number=(
            sub_unit.sub_unit_number if sub_unit else (f"Unit {unit.unit_number}" if unit else "?")
        ),
        unit_id=quiz.unit_id,
        unit_number=unit.unit_number if unit else None,
        unit_title=unit.title if unit else None,
        is_unit_test=quiz.is_unit_test,
        difficulty_level=quiz.difficulty_level,
        status=quiz.status,
        total_questions=quiz.total_questions,
        passing_threshold=quiz.passing_threshold,
        is_practice=quiz.is_practice,
        resumed=resumed,
        answered_count=answered,
        next_question_number=next_number,
        all_answered=next_number is None and bool(questions),
        questions=questions,
    )


def _load_owned_quiz(db: Session, user: User, quiz_id: str) -> Quiz:
    """Load a quiz, confirming it belongs to one of the caller's students."""
    quiz = db.get(Quiz, quiz_id)
    if quiz is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found.")
    # Reuses the ownership rule; raises 404 rather than 403 so quiz ids cannot
    # be enumerated.
    assert_owns_student(db, user, quiz.student_id)
    return quiz


@router.post("/start", response_model=QuizOut, status_code=status.HTTP_201_CREATED)
def start_quiz(
    payload: QuizStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QuizOut:
    """Start a quiz, or hand back the unfinished one.

    Resuming is unconditional: there is no way to discard a half-finished
    attempt. A child who closes the tab mid-quiz and returns tomorrow always
    gets the same questions with their answers intact. Minting a fresh quiz
    instead would strand their work on an orphaned row, burn ten more bank
    questions, and make them redo questions they had already answered
    correctly.
    """
    assert_owns_student(db, user, payload.student_id)

    sub_unit = db.get(CurriculumSubUnit, payload.sub_unit_id)
    if sub_unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-unit not found.")

    existing = find_resumable_quiz(
        db,
        student_id=payload.student_id,
        sub_unit_id=payload.sub_unit_id,
        difficulty=payload.difficulty,
    )
    if existing is not None:
        logger.info("Resuming quiz %s for student %s", existing.id[:8], payload.student_id[:8])
        return _to_student_view(db, existing, sub_unit, resumed=True)

    try:
        quiz = create_quiz(
            db,
            student_id=payload.student_id,
            sub_unit_id=payload.sub_unit_id,
            difficulty=payload.difficulty,
        )
    except UnitLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except QuizServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    quiz.status = QuizStatus.IN_PROGRESS
    quiz.started_at = datetime.now(UTC)
    db.commit()
    db.refresh(quiz)

    return _to_student_view(db, quiz, sub_unit)


@router.post("/unit-test", response_model=QuizOut, status_code=status.HTTP_201_CREATED)
def start_unit_test(
    payload: UnitTestStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QuizOut:
    """Sit a cumulative test covering a whole unit.

    Unlike an ordinary quiz, consecutive questions come from different
    sub-units. That interleaving is the point: a blocked quiz never makes a
    child decide *which* method a question needs, which is the first thing a
    real test asks of them.

    An unfinished test is handed back rather than replaced, on the same promise
    as any other quiz -- a child who closed the laptop mid-paper returns to the
    paper, not to the start of it.
    """
    student = assert_owns_student(db, user, payload.student_id)

    resumable = find_resumable_unit_test(db, student.id, payload.unit_id)
    if resumable is not None:
        return _to_student_view(db, resumable, None, resumed=True)

    try:
        quiz = create_unit_test(
            db,
            student_id=student.id,
            unit_id=payload.unit_id,
            question_count=payload.question_count,
        )
    except QuizServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    quiz.status = QuizStatus.IN_PROGRESS
    quiz.started_at = datetime.now(UTC)
    db.commit()
    return _to_student_view(db, quiz, None)


@router.post("/{quiz_id}/complete-unit-test", response_model=UnitTestResultOut)
def complete_unit_test(
    quiz_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UnitTestResultOut:
    """Score a cumulative test and break the result down by sub-unit.

    The overall percentage is the least useful number here. "18/20, but 1 of 4
    on comparing irrational numbers" is what tells a parent where to look, and
    it is only computable because each question records the sub-unit it came
    from rather than inheriting one from the quiz.
    """
    quiz = _load_owned_quiz(db, user, quiz_id)
    if not quiz.is_unit_test:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This quiz is not a unit test; use /complete instead.",
        )
    if quiz.status is QuizStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This test is already scored."
        )

    responses = db.query(QuizResponse).filter_by(quiz_id=quiz.id).all()
    if len(responses) < quiz.total_questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"{quiz.total_questions - len(responses)} question(s) still " "unanswered."),
        )

    correct = sum(1 for response in responses if response.is_correct)
    breakdown: dict[str, list[int]] = {}
    for response in responses:
        question = db.get(QuizQuestion, response.question_id)
        tag = (question.skill_tag if question else None) or "untagged"
        bucket = breakdown.setdefault(tag, [0, 0])
        bucket[1] += 1
        if response.is_correct:
            bucket[0] += 1

    attempt = record_attempt(
        db,
        quiz=quiz,
        correct_count=correct,
        duration_seconds=quiz.time_spent_seconds,
        skill_breakdown={tag: round(hit / total, 3) for tag, (hit, total) in breakdown.items()},
    )

    per_sub_unit = sub_unit_breakdown(db, quiz)
    sub_units = sub_unit_titles(db, list(per_sub_unit))
    scores = [
        SubUnitScoreOut(
            sub_unit_id=sub_unit_id,
            sub_unit_number=sub_units[sub_unit_id].sub_unit_number,
            sub_unit_title=sub_units[sub_unit_id].title,
            correct=counts["correct"],
            total=counts["total"],
            accuracy=round(counts["correct"] / counts["total"], 3) if counts["total"] else 0.0,
        )
        for sub_unit_id, counts in per_sub_unit.items()
        if sub_unit_id in sub_units
    ]
    scores.sort(key=lambda score: (score.accuracy, score.sub_unit_number))

    # Progress is untouched, so no sub-unit progress is passed to the award
    # engine: a cumulative paper pays for the work, never for completion.
    award = award_for_attempt(db, attempt=attempt, progress=None, hints_used=0)
    unit = db.get(CurriculumUnit, quiz.unit_id)
    db.commit()

    return UnitTestResultOut(
        quiz_id=quiz.id,
        unit_id=quiz.unit_id or "",
        unit_number=unit.unit_number if unit else 0,
        unit_title=unit.title if unit else "(removed)",
        score_percentage=quiz.score_percentage or 0.0,
        correct_count=correct,
        total_questions=quiz.total_questions,
        passing_threshold=quiz.passing_threshold,
        is_passed=quiz.is_passed,
        attempt_number=attempt.attempt_number,
        sub_unit_scores=scores,
        weakest_sub_units=[score.sub_unit_number for score in scores if score.accuracy < 0.7],
        award=AwardOut(
            points_earned=award.points_earned,
            total_points=award.total_points,
            level=award.level,
            levelled_up=award.levelled_up,
            points_to_next_level=award.points_to_next_level,
            current_streak_days=award.current_streak_days,
            streak_extended=award.streak_extended,
            new_badges=[
                BadgeOut(
                    badge_key=badge.badge_key,
                    badge_name=badge.badge_name,
                    description=badge.description,
                    icon=badge.icon,
                    tier=badge.tier,
                    points_awarded=badge.points_awarded,
                    earned=True,
                    unlocked_at=badge.unlocked_at,
                )
                for badge in award.new_badges
            ],
        ),
    )


@router.get("/{quiz_id}", response_model=QuizOut)
def read_quiz(
    quiz_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QuizOut:
    """Re-read a quiz in progress, e.g. after a page refresh."""
    quiz = _load_owned_quiz(db, user, quiz_id)
    sub_unit = db.get(CurriculumSubUnit, quiz.sub_unit_id)
    return _to_student_view(db, quiz, sub_unit, resumed=True)


@router.post("/{quiz_id}/questions/{question_id}/hint", response_model=HintOut)
def request_hint(
    quiz_id: str,
    question_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HintOut:
    """Serve the hint for one question, and record that it was asked for.

    Recording here rather than trusting a client-supplied flag matters: the
    Gap Detector will read ``hint_used`` as evidence that a skill is shaky,
    and a signal the client can fake is not evidence.
    """
    quiz = _load_owned_quiz(db, user, quiz_id)

    question = db.get(QuizQuestion, question_id)
    if question is None or question.quiz_id != quiz.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question not part of this quiz."
        )
    if not question.hint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No hint for this question."
        )

    answered = db.query(QuizResponse).filter_by(quiz_id=quiz.id, question_id=question.id).first()
    if answered is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This question has already been answered.",
        )

    question.hint_requested = True
    db.commit()

    return HintOut(question_id=question.id, hint=question.hint)


@router.post("/{quiz_id}/answer", response_model=AnswerFeedback)
def answer_question(
    quiz_id: str,
    payload: AnswerRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnswerFeedback:
    """Record an answer and return teaching feedback immediately.

    The answer key only ever crosses the wire here, after the student has
    committed. Re-answering the same question is rejected: without that, a
    student could read the correct answer from the first response and resubmit.
    """
    quiz = _load_owned_quiz(db, user, quiz_id)

    if quiz.status is QuizStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This quiz is already complete."
        )

    question = db.get(QuizQuestion, payload.question_id)
    if question is None or question.quiz_id != quiz.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question not part of this quiz."
        )

    existing = db.query(QuizResponse).filter_by(quiz_id=quiz.id, question_id=question.id).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This question has already been answered.",
        )

    selected = payload.selected_answer.strip().upper()
    valid_keys = {str(option.get("key", "")).upper() for option in (question.options or [])}
    if valid_keys and selected not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Answer must be one of {sorted(valid_keys)}.",
        )

    is_correct = selected == question.correct_answer.strip().upper()

    db.add(
        QuizResponse(
            quiz_id=quiz.id,
            question_id=question.id,
            student_id=quiz.student_id,
            selected_answer=selected,
            is_correct=is_correct,
            time_spent_seconds=payload.time_spent_seconds,
            # Server-recorded, set when the hint endpoint actually served it.
            hint_used=question.hint_requested,
            feedback_shown=True,
        )
    )
    if is_correct:
        quiz.correct_count += 1
    quiz.time_spent_seconds += payload.time_spent_seconds
    if quiz.status is QuizStatus.PENDING:
        quiz.status = QuizStatus.IN_PROGRESS
    db.flush()

    answered = db.query(QuizResponse).filter_by(quiz_id=quiz.id).count()
    db.commit()

    # A right answer needs no teaching. Sending the explanation anyway would
    # bury the feedback that matters on the questions they got wrong.
    rationales = question.distractor_rationales or {}
    return AnswerFeedback(
        question_id=question.id,
        is_correct=is_correct,
        correct_answer=None if is_correct else question.correct_answer,
        explanation=None if is_correct else question.explanation,
        why_your_answer_was_wrong=None if is_correct else rationales.get(selected),
        hint_used=question.hint_requested,
        points_earned=question.points if is_correct else 0,
        answered_count=answered,
        total_questions=quiz.total_questions,
        quiz_complete=answered >= quiz.total_questions,
    )


@router.post("/{quiz_id}/complete", response_model=QuizResultOut)
def complete_quiz(
    quiz_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QuizResultOut:
    """Score the quiz and move progress.

    Practice quizzes are scored and recorded but leave progress untouched.
    """
    quiz = _load_owned_quiz(db, user, quiz_id)

    if quiz.status is QuizStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This quiz is already complete."
        )

    answered = db.query(QuizResponse).filter_by(quiz_id=quiz.id).count()
    if answered < quiz.total_questions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{quiz.total_questions - answered} question(s) still unanswered.",
        )

    correct = db.query(QuizResponse).filter_by(quiz_id=quiz.id, is_correct=True).count()
    # Per-skill accuracy, for the Gap Detector agent later.
    breakdown: dict[str, list[int]] = {}
    for response in db.query(QuizResponse).filter_by(quiz_id=quiz.id).all():
        question = db.get(QuizQuestion, response.question_id)
        tag = (question.skill_tag if question else None) or "untagged"
        bucket = breakdown.setdefault(tag, [0, 0])
        bucket[1] += 1
        if response.is_correct:
            bucket[0] += 1

    attempt = record_attempt(
        db,
        quiz=quiz,
        correct_count=correct,
        duration_seconds=quiz.time_spent_seconds,
        skill_breakdown={tag: round(hit / total, 3) for tag, (hit, total) in breakdown.items()},
    )

    progress = (
        db.query(SubUnitProgress)
        .filter_by(student_id=quiz.student_id, sub_unit_id=quiz.sub_unit_id)
        .one_or_none()
    )
    # Reported, not spent. The client acknowledges it via
    # POST /curriculum/students/{id}/celebrations/{sub_unit_id}/ack once the
    # animation has actually played, so a dropped response or a tab closed
    # mid-animation cannot cost a child their 100% moment -- they will see it
    # on the dashboard next time instead.
    celebrate = bool(progress and progress.should_celebrate)

    hints_used = db.query(QuizResponse).filter_by(quiz_id=quiz.id, hint_used=True).count()
    award = award_for_attempt(db, attempt=attempt, progress=progress, hints_used=hints_used)

    db.commit()

    return QuizResultOut(
        quiz_id=quiz.id,
        score_percentage=quiz.score_percentage or 0.0,
        correct_count=correct,
        total_questions=quiz.total_questions,
        passing_threshold=quiz.passing_threshold,
        is_passed=quiz.is_passed,
        is_practice=quiz.is_practice,
        attempt_number=attempt.attempt_number,
        completion_percentage=progress.completion_percentage if progress else 0,
        difficulty_completed=bool(
            progress and getattr(progress, f"{quiz.difficulty_level.value}_completed", False)
        ),
        sub_unit_complete=bool(progress and progress.is_complete),
        should_celebrate=celebrate,
        next_difficulty=progress.next_difficulty if progress else None,
        award=AwardOut(
            points_earned=award.points_earned,
            total_points=award.total_points,
            level=award.level,
            levelled_up=award.levelled_up,
            points_to_next_level=award.points_to_next_level,
            current_streak_days=award.current_streak_days,
            streak_extended=award.streak_extended,
            new_badges=[
                BadgeOut(
                    badge_key=badge.badge_key,
                    badge_name=badge.badge_name,
                    description=badge.description,
                    icon=badge.icon,
                    tier=badge.tier,
                    points_awarded=badge.points_awarded,
                    earned=True,
                    unlocked_at=badge.unlocked_at,
                )
                for badge in award.new_badges
            ],
            avatars_unlocked=award.avatars_unlocked,
            breakdown=award.breakdown,
        ),
    )
