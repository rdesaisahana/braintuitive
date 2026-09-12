"""Pydantic request and response models.

One rule shapes this whole module: **a question sent to a student must not
carry its own answer.** ``QuestionForStudent`` deliberately has no
``correct_answer``, no ``explanation`` and no ``distractor_rationales``. Those
appear only in ``AnswerFeedback``, returned after the student has committed to
an answer.

It would be far simpler to serialise the ORM object once and reuse it. It
would also mean the answer key sits in the browser's network tab, and the
mastery signal the whole progression rests on would be worthless.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from db.models import DifficultyLevel, ProgressStatus, QuizStatus, UserRole

# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class SignupRequest(BaseModel):
    """Create a parent or teacher account."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=1, max_length=150)
    role: Literal["parent", "teacher"] = "parent"
    timezone: str = Field(default="America/New_York", max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# Students
# --------------------------------------------------------------------------- #


class StudentCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    grade_level: int = Field(ge=1, le=12)
    date_of_birth: date | None = None
    school_name: str | None = Field(default=None, max_length=150)
    # A starter the child picks. Omitted falls back to the first starter --
    # nobody should have to choose a character before they know what the app
    # is, and they can change it any time from the shop.
    avatar_key: str | None = Field(default=None, max_length=60)


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    first_name: str
    last_name: str | None
    grade_level: int
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# Curriculum
# --------------------------------------------------------------------------- #


class SubUnitOut(BaseModel):
    """A sub-unit with this student's progress folded in."""

    id: str
    sub_unit_number: str
    title: str
    description: str | None = None
    sequence: int

    completion_percentage: int = 0
    status: ProgressStatus = ProgressStatus.NOT_STARTED
    beginner_completed: bool = False
    intermediate_completed: bool = False
    proficient_completed: bool = False
    next_difficulty: DifficultyLevel | None = None
    # True until the client acknowledges it. Reporting does not spend it, so a
    # lost response or a closed tab cannot lose a child's 100% moment.
    should_celebrate: bool = False
    # Difficulties whose bank can fill a quiz instantly. A tier missing here
    # still works, but falls back to live generation and takes ~2 minutes --
    # the UI needs to say so rather than freeze a button.
    ready_difficulties: list[DifficultyLevel] = Field(default_factory=list)


class NextActionOut(BaseModel):
    """What this student should do next.

    The rule lives here rather than in the frontend so the two cannot drift.

    ``action`` is what the client should actually do:

    * ``resume`` -- an unfinished quiz is waiting; open ``resume_quiz_id``.
    * ``start``  -- begin a new quiz on the named sub-unit and difficulty.
    * ``none``   -- nothing available (no curriculum, or everything done).
    """

    has_next: bool
    action: Literal["resume", "start", "none"] = "none"
    # Set when action is "resume": the quiz to reopen.
    resume_quiz_id: str | None = None
    answered_count: int = 0
    next_question_number: int | None = None
    unit_id: str | None = None
    unit_number: int | None = None
    unit_title: str | None = None
    sub_unit_id: str | None = None
    sub_unit_number: str | None = None
    sub_unit_title: str | None = None
    difficulty: DifficultyLevel | None = None
    quiz_ready: bool = False
    message: str = ""


class UnitOut(BaseModel):
    """A unit, its sub-units, and whether the student may enter it."""

    id: str
    unit_number: int
    title: str
    description: str | None = None
    total_sub_units: int

    unlocked: bool
    lock_reason: str | None = None
    blocking_sub_units: list[str] = Field(default_factory=list)
    completion_percentage: int = 0
    sub_units: list[SubUnitOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Gamification
# --------------------------------------------------------------------------- #


class BadgeOut(BaseModel):
    """A badge, earned or still locked."""

    badge_key: str
    badge_name: str
    description: str | None = None
    icon: str | None = None
    tier: str
    points_awarded: int
    earned: bool = False
    unlocked_at: datetime | None = None


class GamificationProfileOut(BaseModel):
    """Points, level, streak and avatar for one student."""

    student_id: str
    student_name: str
    total_points: int
    level: int
    points_to_next_level: int
    # 0.0-1.0 through the current level, for a progress bar.
    level_progress: float = Field(ge=0.0, le=1.0)

    current_streak_days: int
    longest_streak_days: int
    last_activity_date: date | None = None

    # Lifetime earnings drive the level; the balance is what is left to
    # spend after redemptions. Buying never lowers the level.
    points_spent: int = 0
    points_balance: int = 0

    avatar_key: str
    # Carried on the profile so every screen can draw the avatar without a
    # second request to the shop just to look up one emoji.
    avatar_image: str = ""
    avatar_name: str = ""
    unlocked_avatars: list[str] = Field(default_factory=list)

    total_quizzes_completed: int
    total_correct_answers: int
    total_sub_units_completed: int
    total_units_completed: int

    badges_earned: int
    badges_total: int


class AvatarSelectRequest(BaseModel):
    avatar_key: str = Field(min_length=1, max_length=60)


class AvatarOut(BaseModel):
    """One character in the shop, as this child sees it."""

    key: str
    name: str
    image: str
    price: int
    blurb: str
    is_starter: bool
    owned: bool
    affordable: bool


class AvatarShopOut(BaseModel):
    """The shop: everything on offer, plus what this child can spend."""

    points_balance: int
    total_points: int
    points_spent: int
    wearing: str
    avatars: list[AvatarOut] = Field(default_factory=list)


class AwardOut(BaseModel):
    """What a completed quiz earned, for the celebration screen."""

    points_earned: int
    total_points: int
    level: int
    levelled_up: bool
    points_to_next_level: int
    current_streak_days: int
    streak_extended: bool
    new_badges: list[BadgeOut] = Field(default_factory=list)
    avatars_unlocked: list[str] = Field(default_factory=list)
    breakdown: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Quiz
# --------------------------------------------------------------------------- #


class QuizStartRequest(BaseModel):
    student_id: str
    sub_unit_id: str
    difficulty: DifficultyLevel


class OptionOut(BaseModel):
    key: str
    text: str


class AnsweredState(BaseModel):
    """What the student already did on a question, and what they were shown.

    Safe to send: they have committed, and this is only ever the feedback they
    have already seen. ``correct_answer`` and ``explanation`` stay null when
    they got it right -- a correct answer needs no explanation.
    """

    selected_answer: str | None = None
    is_correct: bool
    hint_used: bool = False
    correct_answer: str | None = None
    explanation: str | None = None
    why_your_answer_was_wrong: str | None = None


class QuestionForStudent(BaseModel):
    """A question as the student sees it.

    No ``correct_answer``, no ``explanation``, no ``distractor_rationales`` --
    and no ``hint``. Shipping the hint with the question would remove the
    productive struggle that makes the hint worth having, so only a flag is
    sent and the text is fetched on demand.
    """

    id: str
    question_number: int
    question_text: str
    # "multiple_choice" (options A-D) or "true_false" (A "True", B "False").
    question_type: str = "multiple_choice"
    options: list[OptionOut]
    has_hint: bool = False
    points: int

    # Present only for questions the student has ALREADY answered, so a quiz
    # picked up the next day looks as they left it.
    #
    # Nested rather than flattened on purpose: as a sub-object it is null for
    # an unanswered question, so the strings "correct_answer" and
    # "explanation" do not appear in the payload at all. Flattened, those
    # field names would ship with every question -- harmless today, and
    # exactly the shape a future bug fills in by accident.
    answered: AnsweredState | None = None


class HintOut(BaseModel):
    """A hint, served only when the student asks for it."""

    question_id: str
    hint: str


class QuizOut(BaseModel):
    id: str
    student_id: str
    # Null on a cumulative unit test, which is scoped to the unit instead.
    sub_unit_id: str | None = None
    sub_unit_number: str
    unit_id: str | None = None
    unit_number: int | None = None
    unit_title: str | None = None
    difficulty_level: DifficultyLevel
    status: QuizStatus
    total_questions: int
    passing_threshold: float
    is_practice: bool
    is_unit_test: bool = False

    # --- resume ----------------------------------------------------------
    # True when this quiz already existed and was handed back rather than
    # created fresh.
    resumed: bool = False
    answered_count: int = 0
    # Where to put the child when they open it. None once everything is
    # answered and only the submit remains.
    next_question_number: int | None = None
    all_answered: bool = False

    questions: list[QuestionForStudent]


class AnswerRequest(BaseModel):
    question_id: str
    selected_answer: str = Field(min_length=1, max_length=500)
    time_spent_seconds: int = Field(default=0, ge=0)
    # No hint_used here on purpose: the server records that when the hint is
    # actually served, so it cannot be misreported.


class AnswerFeedback(BaseModel):
    """Khan-style feedback, returned the instant the student answers.

    A correct answer gets no explanation. Explaining something the student
    just demonstrated they understand is noise, and it buries the feedback
    that does matter. ``correct_answer``, ``explanation`` and
    ``why_your_answer_was_wrong`` are therefore populated only when they got
    it wrong.
    """

    question_id: str
    is_correct: bool
    correct_answer: str | None = None
    explanation: str | None = None
    # Why the option they picked was wrong. Absent when they were right.
    why_your_answer_was_wrong: str | None = None
    hint_used: bool = False
    points_earned: int

    answered_count: int
    total_questions: int
    quiz_complete: bool


class QuizResultOut(BaseModel):
    """The outcome of a completed quiz."""

    quiz_id: str
    score_percentage: float
    correct_count: int
    total_questions: int
    passing_threshold: float
    is_passed: bool
    is_practice: bool
    attempt_number: int

    completion_percentage: int
    difficulty_completed: bool
    sub_unit_complete: bool
    should_celebrate: bool
    next_difficulty: DifficultyLevel | None = None
    # Points, level and badges earned by this attempt. Practice attempts
    # return a report with zero points rather than nothing, so the UI can say
    # "practice - no points" instead of showing an empty space.
    award: AwardOut | None = None


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


class SubUnitProgressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sub_unit_id: str
    sub_unit_number: str
    title: str
    completion_percentage: int
    status: ProgressStatus
    beginner_best_score: float
    intermediate_best_score: float
    proficient_best_score: float
    total_attempts: int


class SkillStatOut(BaseModel):
    """Per-skill accuracy, aggregated from individual answers.

    This is the raw material the Gap Detector agent will work from, and the
    most useful thing a parent can see: not "72% overall" but *which* skill is
    shaky.
    """

    skill_tag: str
    questions_answered: int
    correct: int
    accuracy: float = Field(ge=0.0, le=1.0)
    hints_used: int
    needs_attention: bool


class AttemptOut(BaseModel):
    """One completed quiz, for the history list."""

    id: str
    unit_number: int | None = None
    # Lets the history link straight to that topic's revision review.
    sub_unit_id: str | None = None
    sub_unit_number: str
    sub_unit_title: str
    difficulty_level: DifficultyLevel
    attempt_number: int
    score_percentage: float
    correct_count: int
    total_questions: int
    is_passed: bool
    is_practice: bool
    duration_seconds: int
    completed_at: datetime


class ProgressSummaryOut(BaseModel):
    """The parent dashboard headline."""

    student_id: str
    student_name: str
    grade_level: int
    current_unit: int | None = None
    units_completed: int
    sub_units_completed: int
    sub_units_total: int
    overall_percentage: float

    quizzes_completed: int
    practice_quizzes: int
    average_score: float
    total_time_seconds: int
    current_streak_days: int
    last_active: datetime | None = None
    # Sub-units at 100% whose celebration the child has not yet been shown.
    celebrations_pending: int = 0

    sub_units: list[SubUnitProgressOut] = Field(default_factory=list)


class ReviewQuestionOut(BaseModel):
    """One question worth looking at again, and what happened last time.

    Carries nothing the child was not already shown when they answered: the
    correct answer and the explanation only for a question they got wrong,
    exactly as the live feedback gave them, and the hint only when they asked
    for it. A question answered right keeps its explanation to itself here too.
    """

    question_id: str
    question_text: str
    question_type: str = "multiple_choice"
    options: list[OptionOut] = Field(default_factory=list)
    difficulty_level: DifficultyLevel
    skill_tag: str | None = None
    selected_answer: str | None = None
    is_correct: bool
    hint_used: bool
    correct_answer: str | None = None
    explanation: str | None = None
    why_your_answer_was_wrong: str | None = None
    hint: str | None = None
    # Got wrong once, but answered correctly on a later attempt.
    since_answered_correctly: bool = False
    answered_at: datetime


class RevisionPointOut(BaseModel):
    """One skill in the topic that needs another look, and why."""

    skill_tag: str
    skill_name: str
    answered: int
    wrong: int
    hints_used: int
    message: str


class TopicReviewOut(BaseModel):
    """What to revise in one topic, built from the child's own answers.

    Worked out by rules, not by a model: it is instant, costs nothing, and says
    the same thing every time it is opened.
    """

    sub_unit_id: str
    sub_unit_number: str
    title: str
    unit_number: int | None = None
    unit_title: str | None = None
    completion_percentage: int
    questions_answered: int
    summary: str
    # The level with the most mistakes: where to go back to.
    revise_level: DifficultyLevel | None = None
    revise: list[RevisionPointOut] = Field(default_factory=list)
    missed: list[ReviewQuestionOut] = Field(default_factory=list)
    hinted: list[ReviewQuestionOut] = Field(default_factory=list)


class SubUnitDetailOut(BaseModel):
    """One sub-unit, with the attempt history behind it."""

    sub_unit_id: str
    sub_unit_number: str
    title: str
    unit_number: int | None = None
    completion_percentage: int
    status: ProgressStatus
    beginner_best_score: float
    intermediate_best_score: float
    proficient_best_score: float
    next_difficulty: DifficultyLevel | None = None
    total_attempts: int
    total_time_seconds: int
    attempts: list[AttemptOut] = Field(default_factory=list)
    skills: list[SkillStatOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Learning gaps
# --------------------------------------------------------------------------- #


class GapOut(BaseModel):
    """One diagnosed gap.

    ``SkillStatOut`` says a child scores 0/6 on ``subtract_integers``. This
    says *why*: the wrong options they chose all mean "found the difference
    but used the wrong sign". A parent can act on the second; the first only
    tells them to worry.
    """

    skill_tag: str
    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    severity: Literal["critical", "moderate", "minor"]
    accuracy: float = Field(ge=0.0, le=1.0)
    questions_answered: int
    hints_used: int
    evidence: list[str] = Field(
        default_factory=list,
        description="The specific wrong answers behind this finding, counted.",
    )
    likely_misconception: str
    recommendation: str
    recommended_difficulty: DifficultyLevel


class GapReportOut(BaseModel):
    """The full analysis for one student.

    ``gaps`` being empty is a real answer, not a failure: it means every skill
    with enough evidence is above the attention threshold. ``skills_with_thin_
    evidence`` names the skills deliberately *not* judged, so a parent can see
    that silence on a skill means "too few answers yet", not "fine".
    """

    student_id: str
    student_name: str
    summary: str
    gaps: list[GapOut] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    skills_analysed: int = 0
    responses_analysed: int = 0
    skills_with_thin_evidence: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Curriculum upload
# --------------------------------------------------------------------------- #


class CurriculumPreviewUnitOut(BaseModel):
    unit_number: int
    title: str
    topics: int


class CurriculumPreviewOut(BaseModel):
    """What the parser found, shown before anything is built.

    Concrete on purpose: "is this the right file?" is answerable against
    "Grade 6 -- Unit 1: Number Fluency", and not against a filename.
    """

    title: str
    #: 0 when the cover page did not state a grade.
    grade_level: int
    page_count: int
    total_units: int
    total_topics: int
    units: list[CurriculumPreviewUnitOut] = Field(default_factory=list)


class CurriculumUploadOut(BaseModel):
    """One curriculum ingestion, in progress or finished.

    Polled by the upload screen. ``error`` is written for the parent -- "we
    could not find any units in that PDF" -- rather than being an exception
    string, because the person reading it is the only one who can fix the
    input.
    """

    id: str
    filename: str
    subject: str
    grade_level: int | None = None
    status: Literal["pending", "processing", "review", "completed", "failed", "cancelled"]
    error: str | None = None
    units_written: int = 0
    sub_units_written: int = 0
    chunks_created: int = 0
    vectors_upserted: int = 0
    warnings: list[str] = Field(default_factory=list)
    #: Present once the PDF has been read; drives the confirmation step.
    preview: CurriculumPreviewOut | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class CurriculumStatusOut(BaseModel):
    """Whether this family is working from their own curriculum yet."""

    has_own_curriculum: bool
    units: int = Field(description="Units in the curriculum currently in use.")
    using_sample: bool = Field(
        description="True while they are still on the shared sample curriculum.",
    )
    active_upload: CurriculumUploadOut | None = None
    filename: str | None = Field(
        default=None,
        description=(
            "The PDF the curriculum in use came from, so the interface can show "
            "it attached in the file picker rather than in a separate panel."
        ),
    )


class CurriculumDeletionOut(BaseModel):
    """What removing a curriculum would take, or did take.

    Counted rather than described, because "are you sure?" against an unnamed
    quantity is not consent. Points and badges are absent from this list on
    purpose: they hang off the child, not the curriculum, and survive.
    """

    units: int = 0
    sub_units: int = 0
    quizzes: int = 0
    progress_rows: int = 0
    bank_questions: int = 0
    uploads: int = 0
    vectors_dropped: bool = False


# --------------------------------------------------------------------------- #
# Cumulative unit tests
# --------------------------------------------------------------------------- #


class UnitTestStartRequest(BaseModel):
    """Sit a cumulative test over one whole unit."""

    student_id: str
    unit_id: str
    question_count: int = Field(default=20, ge=4, le=60)


class SubUnitScoreOut(BaseModel):
    """How one sub-unit fared on a cumulative paper.

    The reason for sitting one: not the overall score but which parts of the
    unit it exposes.
    """

    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    correct: int
    total: int
    accuracy: float = Field(ge=0.0, le=1.0)


class UnitTestResultOut(BaseModel):
    """The outcome of a cumulative test.

    Carries no ``completion_percentage`` or ``next_difficulty``, because a unit
    test moves no progress -- a mixed paper cannot say which tier of which
    sub-unit a child has cleared.
    """

    quiz_id: str
    unit_id: str
    unit_number: int
    unit_title: str
    score_percentage: float
    correct_count: int
    total_questions: int
    passing_threshold: float
    is_passed: bool
    attempt_number: int
    sub_unit_scores: list[SubUnitScoreOut] = Field(default_factory=list)
    weakest_sub_units: list[str] = Field(default_factory=list)
    award: AwardOut | None = None


# --------------------------------------------------------------------------- #
# Test preparation
# --------------------------------------------------------------------------- #


class StudyPlanRequest(BaseModel):
    """What the test covers and how long there is to prepare."""

    unit_numbers: list[int] | None = Field(
        default=None,
        description=(
            "Units the test covers. Omitted means every unit the child has "
            "actually worked in -- revising a unit they never opened is not "
            "revision."
        ),
    )
    days_until_test: int = Field(default=7, ge=1, le=30)
    question_budget: int = Field(
        default=30,
        ge=2,
        le=120,
        description="A ceiling, not a quota: a child who is solid gets a shorter plan.",
    )


class DrillBlockOut(BaseModel):
    """One slice of revision the child can start directly."""

    sub_unit_id: str
    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    difficulty: DifficultyLevel
    question_count: int
    risk: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    bank_ready: bool = Field(
        default=True,
        description="False means these questions must be generated live, taking minutes.",
    )


class StudySessionOut(BaseModel):
    """One evening's work."""

    day: int
    focus: str
    question_count: int
    blocks: list[DrillBlockOut] = Field(default_factory=list)


class TopicRiskOut(BaseModel):
    """Why a topic did or did not make the plan.

    Returned in full, including topics that were left out, so a parent can see
    the reasoning rather than being handed a schedule to take on faith.
    """

    sub_unit_number: str
    sub_unit_title: str
    unit_number: int
    risk: float = Field(ge=0.0, le=1.0)
    never_attempted: bool
    completion_percentage: int
    days_since_practice: int | None = None
    weakest_skill: str | None = None
    reasons: list[str] = Field(default_factory=list)


class StudyPlanOut(BaseModel):
    """A revision plan for a specific test on a specific date."""

    student_id: str
    student_name: str
    unit_numbers: list[int] = Field(default_factory=list)
    days_until_test: int
    total_questions: int
    summary: str
    advice: list[str] = Field(default_factory=list)
    sessions: list[StudySessionOut] = Field(default_factory=list)
    risks: list[TopicRiskOut] = Field(default_factory=list)
    topics_not_yet_started: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rehearsal_unit_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Units worth sitting the cumulative test on once the drills are "
            "done. Drills are blocked by construction; the last night before a "
            "test is when a child needs one interleaved paper instead."
        ),
    )


# --------------------------------------------------------------------------- #
# How points work
# --------------------------------------------------------------------------- #


class EarningRuleOut(BaseModel):
    """One way to earn points, described for the child."""

    key: str
    points: int
    label: str
    detail: str


class PointsGuideOut(BaseModel):
    """What points are for, and how to get them.

    Served rather than written into the interface so the numbers a child is
    shown are the numbers the award engine actually pays.
    """

    earning: list[EarningRuleOut] = Field(default_factory=list)
    #: Roughly what a strong ten-question quiz is worth, for "how many quizzes
    #: is that?" arithmetic on the rewards screen.
    quiz_worth: int
    cheapest_avatar_price: int
    spend_on: str


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ErrorDetail(BaseModel):
    detail: str
    code: str | None = None
    context: dict[str, Any] | None = None


__all__ = [
    "AnswerFeedback",
    "AnsweredState",
    "AnswerRequest",
    "CurriculumDeletionOut",
    "CurriculumPreviewOut",
    "CurriculumPreviewUnitOut",
    "CurriculumStatusOut",
    "CurriculumUploadOut",
    "EarningRuleOut",
    "ErrorDetail",
    "PointsGuideOut",
    "GapOut",
    "GapReportOut",
    "GamificationProfileOut",
    "HintOut",
    "LoginRequest",
    "NextActionOut",
    "OptionOut",
    "ProgressSummaryOut",
    "QuestionForStudent",
    "QuizOut",
    "QuizResultOut",
    "QuizStartRequest",
    "RefreshRequest",
    "SignupRequest",
    "AttemptOut",
    "AvatarOut",
    "AvatarSelectRequest",
    "AvatarShopOut",
    "AwardOut",
    "BadgeOut",
    "SkillStatOut",
    "StudyPlanOut",
    "StudyPlanRequest",
    "StudySessionOut",
    "DrillBlockOut",
    "TopicRiskOut",
    "StudentCreate",
    "StudentOut",
    "SubUnitOut",
    "SubUnitDetailOut",
    "SubUnitProgressOut",
    "SubUnitScoreOut",
    "UnitTestResultOut",
    "UnitTestStartRequest",
    "TokenPair",
    "UnitOut",
    "UserOut",
]
