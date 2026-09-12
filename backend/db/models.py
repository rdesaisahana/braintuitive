"""SQLAlchemy ORM models for Braintuitive.

Fourteen tables covering accounts, curriculum, quizzing, progress,
gamification and the pre-generated question bank. Primary keys are string
UUIDs so that records can be created client-side or merged across databases
without integer-sequence collisions.

Progression rules encoded here:
  * A sub-unit is worth 0 / 33 / 67 / 100 % -- one third per difficulty level.
  * A difficulty level counts as complete only when its own threshold is met
    (beginner 70 %, intermediate 80 %, proficient 90 %).
  * A unit is locked until every sub-unit of the previous unit is at 100 %.
  * ``celebration_shown`` guarantees the 100 % celebration fires exactly once.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# --------------------------------------------------------------------------- #
# Base & helpers
# --------------------------------------------------------------------------- #


class Base(DeclarativeBase):
    """Declarative base for every Braintuitive model."""


def _uuid() -> str:
    """Generate a string UUID4 primary key."""
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    """Timezone-aware UTC now (``datetime.utcnow`` is deprecated in 3.12+)."""
    return datetime.now(UTC)


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` to a model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


def _enum_column(python_enum: type[enum.Enum], **kwargs: Any) -> Any:
    """Build an Enum column that stores the *value* rather than the member name.

    Storing ``"beginner"`` instead of ``"BEGINNER"`` keeps the raw SQLite file
    readable and matches the strings used across the API surface.
    """
    return mapped_column(
        SAEnum(
            python_enum,
            values_callable=lambda members: [m.value for m in members],
            native_enum=False,  # SQLite has no native ENUM; use VARCHAR + CHECK
        ),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class UserRole(str, enum.Enum):
    """Who owns the account."""

    PARENT = "parent"
    TEACHER = "teacher"
    ADMIN = "admin"


class DifficultyLevel(str, enum.Enum):
    """The three tiers every sub-unit is quizzed at."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    PROFICIENT = "proficient"

    @property
    def threshold(self) -> float:
        """Score (%) required to pass this difficulty."""
        return {"beginner": 70.0, "intermediate": 80.0, "proficient": 90.0}[self.value]

    @property
    def order(self) -> int:
        """Display/progression order, 0-indexed."""
        return {"beginner": 0, "intermediate": 1, "proficient": 2}[self.value]


class QuizStatus(str, enum.Enum):
    """Lifecycle of a single quiz instance."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ProgressStatus(str, enum.Enum):
    """Lifecycle of a student's work on one sub-unit."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class NotificationType(str, enum.Enum):
    """Why we are contacting the parent/teacher."""

    DAILY_REMINDER = "daily_reminder"
    WEEKLY_REPORT = "weekly_report"
    ACHIEVEMENT_UNLOCK = "achievement_unlock"
    UNIT_COMPLETE = "unit_complete"


class NotificationChannel(str, enum.Enum):
    """How the notification is delivered."""

    EMAIL = "email"
    IN_APP = "in_app"


class QuestionType(str, enum.Enum):
    """Supported question formats."""

    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    NUMERIC = "numeric"
    SHORT_ANSWER = "short_answer"


# --------------------------------------------------------------------------- #
# 1. users
# --------------------------------------------------------------------------- #


class User(Base, TimestampMixin):
    """A parent, teacher or admin account. Owns one or more students."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[UserRole] = _enum_column(
        UserRole, default=UserRole.PARENT, nullable=False, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(30))
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_opt_in: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    curriculum_units: Mapped[list[CurriculumUnit]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    curriculum_uploads: Mapped[list[CurriculumUpload]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    students: Mapped[list[Student]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", lazy="selectin"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.email} ({self.role.value})>"


# --------------------------------------------------------------------------- #
# 2. students
# --------------------------------------------------------------------------- #


class Student(Base, TimestampMixin):
    """A child profile. All learning data hangs off this row."""

    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(80))
    grade_level: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    school_name: Mapped[str | None] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    parent: Mapped[User] = relationship(back_populates="students")
    quizzes: Mapped[list[Quiz]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    progress: Mapped[list[SubUnitProgress]] = relationship(
        back_populates="student", cascade="all, delete-orphan", lazy="selectin"
    )
    attempts: Mapped[list[QuizAttempt]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    badges: Mapped[list[AchievementBadge]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    gamification_profile: Mapped[GamificationProfile | None] = relationship(
        back_populates="student", cascade="all, delete-orphan", uselist=False
    )

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() if self.last_name else self.first_name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Student {self.display_name} grade={self.grade_level}>"


# --------------------------------------------------------------------------- #
# 3. curriculum_units
# --------------------------------------------------------------------------- #


class CurriculumUnit(Base, TimestampMixin):
    """A top-level unit (Unit 1, Unit 2, ...). Unlocked sequentially."""

    __tablename__ = "curriculum_units"
    __table_args__ = (
        # Scoped by owner: two families may each upload their own grade-6 maths
        # guide, and those are different curricula that happen to share a
        # (subject, grade, unit) triple.
        UniqueConstraint(
            "user_id", "subject", "grade_level", "unit_number", name="uq_unit_identity"
        ),
        Index("ix_unit_subject_grade", "subject", "grade_level"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The parent who uploaded this curriculum. NULL means the shared sample
    # curriculum, which every family sees until they upload their own -- a new
    # account with an empty dashboard would look broken rather than new.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    unit_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    subject: Mapped[str] = mapped_column(String(80), default="math", nullable=False)
    grade_level: Mapped[int] = mapped_column(Integer, nullable=False)

    # Provenance: which PDF this unit was parsed out of.
    source_document: Mapped[str | None] = mapped_column(String(255))
    source_page_start: Mapped[int | None] = mapped_column(Integer)
    source_page_end: Mapped[int | None] = mapped_column(Integer)

    total_sub_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    owner: Mapped[User | None] = relationship(back_populates="curriculum_units")
    quizzes: Mapped[list[Quiz]] = relationship(back_populates="unit", cascade="all, delete-orphan")
    sub_units: Mapped[list[CurriculumSubUnit]] = relationship(
        back_populates="unit",
        cascade="all, delete-orphan",
        order_by="CurriculumSubUnit.sequence",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Unit {self.unit_number}: {self.title}>"


# --------------------------------------------------------------------------- #
# 4. curriculum_sub_units
# --------------------------------------------------------------------------- #


class CurriculumSubUnit(Base, TimestampMixin):
    """A sub-unit (1.1, 1.2, 2.3...). Order within a unit is flexible."""

    __tablename__ = "curriculum_sub_units"
    __table_args__ = (
        UniqueConstraint("unit_id", "sub_unit_number", name="uq_sub_unit_identity"),
        Index("ix_sub_unit_unit_sequence", "unit_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    unit_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_units.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sub_unit_number: Mapped[str] = mapped_column(String(20), nullable=False)  # "1.2"
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # ["Add integers with unlike signs", ...] - drives quiz generation prompts.
    learning_objectives: Mapped[list | None] = mapped_column(JSON, default=list)
    skill_tags: Mapped[list | None] = mapped_column(JSON, default=list)

    source_page_start: Mapped[int | None] = mapped_column(Integer)
    source_page_end: Mapped[int | None] = mapped_column(Integer)

    # RAG bookkeeping: where this sub-unit's chunks live in Pinecone.
    vector_namespace: Mapped[str | None] = mapped_column(String(120))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_indexed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    unit: Mapped[CurriculumUnit] = relationship(back_populates="sub_units")
    quizzes: Mapped[list[Quiz]] = relationship(
        back_populates="sub_unit", cascade="all, delete-orphan"
    )
    progress: Mapped[list[SubUnitProgress]] = relationship(
        back_populates="sub_unit", cascade="all, delete-orphan"
    )
    bank_questions: Mapped[list[QuestionBankItem]] = relationship(
        back_populates="sub_unit", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SubUnit {self.sub_unit_number}: {self.title}>"


# --------------------------------------------------------------------------- #
# 5. quizzes
# --------------------------------------------------------------------------- #


class Quiz(Base, TimestampMixin):
    """One quiz instance, scoped to either a single sub-unit or a whole unit.

    Ordinary quizzes carry ``sub_unit_id``. A cumulative unit test carries
    ``unit_id`` instead and draws its questions from every sub-unit in that
    unit -- which is why exactly one of the two is set, and why the check
    constraint enforces it rather than leaving the invariant to convention.
    """

    __tablename__ = "quizzes"
    __table_args__ = (
        Index("ix_quiz_student_subunit", "student_id", "sub_unit_id", "difficulty_level"),
        CheckConstraint(
            "score_percentage IS NULL OR (score_percentage >= 0 AND score_percentage <= 100)",
            name="ck_quiz_score_range",
        ),
        # A quiz belongs to one scope or the other, never both and never
        # neither. Without this, a unit test that also names a sub-unit would
        # silently move that sub-unit's progress.
        CheckConstraint(
            "(sub_unit_id IS NOT NULL AND unit_id IS NULL) "
            "OR (sub_unit_id IS NULL AND unit_id IS NOT NULL)",
            name="ck_quiz_scope_exactly_one",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Exactly one of these is set; see ck_quiz_scope_exactly_one.
    sub_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("curriculum_sub_units.id", ondelete="CASCADE"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("curriculum_units.id", ondelete="CASCADE"), index=True
    )

    difficulty_level: Mapped[DifficultyLevel] = _enum_column(DifficultyLevel, nullable=False)
    status: Mapped[QuizStatus] = _enum_column(
        QuizStatus, default=QuizStatus.PENDING, nullable=False, index=True
    )

    total_questions: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    correct_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_percentage: Mapped[float | None] = mapped_column(Float)
    passing_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    is_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # A retry of a tier the student has already passed. Recorded for history
    # and analytics, but never allowed to move progress -- the point of
    # practice is that it cannot cost you anything.
    is_practice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # A cumulative test over a whole unit. Like practice, it never moves
    # progress: sub-unit progress is earned by clearing that sub-unit's three
    # tiers, and a mixed paper cannot say which of them a child has cleared.
    is_unit_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_spent_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Which model / RAG chunks produced this quiz - needed for auditing whether
    # questions were genuinely grounded in the curriculum.
    generation_metadata: Mapped[dict | None] = mapped_column(JSON, default=dict)

    student: Mapped[Student] = relationship(back_populates="quizzes")
    sub_unit: Mapped[CurriculumSubUnit | None] = relationship(back_populates="quizzes")
    unit: Mapped[CurriculumUnit | None] = relationship(back_populates="quizzes")
    questions: Mapped[list[QuizQuestion]] = relationship(
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.question_number",
        lazy="selectin",
    )
    responses: Mapped[list[QuizResponse]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan"
    )
    attempt: Mapped[QuizAttempt | None] = relationship(
        back_populates="quiz", cascade="all, delete-orphan", uselist=False
    )

    def compute_score(self) -> float:
        """Recalculate ``score_percentage`` and ``is_passed`` from responses."""
        if not self.total_questions:
            return 0.0
        self.score_percentage = round(100.0 * self.correct_count / self.total_questions, 2)
        self.is_passed = self.score_percentage >= self.passing_threshold
        return self.score_percentage

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Quiz {self.difficulty_level.value} {self.status.value} "
            f"score={self.score_percentage}>"
        )


# --------------------------------------------------------------------------- #
# 6. quiz_questions
# --------------------------------------------------------------------------- #


class QuizQuestion(Base, TimestampMixin):
    """A single generated question, grounded in retrieved curriculum chunks."""

    __tablename__ = "quiz_questions"
    __table_args__ = (UniqueConstraint("quiz_id", "question_number", name="uq_question_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quiz_id: Mapped[str] = mapped_column(
        ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True
    )

    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = _enum_column(
        QuestionType, default=QuestionType.MULTIPLE_CHOICE, nullable=False
    )

    # [{"key": "A", "text": "-7"}, ...]
    options: Mapped[list | None] = mapped_column(JSON, default=list)
    correct_answer: Mapped[str] = mapped_column(String(500), nullable=False)

    # Khan-Academy-style: shown the instant the student answers.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    # Per-option "why this is wrong" text: {"B": "You subtracted instead of...", ...}
    distractor_rationales: Mapped[dict | None] = mapped_column(JSON, default=dict)
    hint: Mapped[str | None] = mapped_column(Text)
    # Set when the student actually asks for the hint. Recorded server-side
    # rather than taken from the client, because the Gap Detector will treat
    # "needed a hint" as evidence of a shaky skill.
    hint_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    difficulty_level: Mapped[DifficultyLevel] = _enum_column(DifficultyLevel, nullable=False)
    skill_tag: Mapped[str | None] = mapped_column(String(120), index=True)
    points: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    # Which sub-unit this question teaches. Redundant with the quiz for an
    # ordinary quiz, and the only source of truth for a cumulative unit test,
    # where consecutive questions deliberately come from different sub-units.
    # ondelete=SET NULL so retiring curriculum never destroys answer history.
    sub_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("curriculum_sub_units.id", ondelete="SET NULL"), index=True
    )

    # Pinecone vector ids the generator was grounded on (anti-hallucination audit).
    source_chunk_ids: Mapped[list | None] = mapped_column(JSON, default=list)

    # Which bank item this question was copied from, when it was drawn from the
    # pre-generated pool rather than produced live. Lets a retry hand the
    # student questions they have not seen before. NULL for live generation.
    # ondelete=SET NULL so retiring a bank item never destroys answer history.
    bank_question_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_bank.id", ondelete="SET NULL"), index=True
    )

    quiz: Mapped[Quiz] = relationship(back_populates="questions")
    responses: Mapped[list[QuizResponse]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Question #{self.question_number} {self.question_text[:40]!r}>"


# --------------------------------------------------------------------------- #
# 7. quiz_responses
# --------------------------------------------------------------------------- #


class QuizResponse(Base, TimestampMixin):
    """A student's answer to one question, plus the feedback interaction."""

    __tablename__ = "quiz_responses"
    __table_args__ = (Index("ix_response_student_question", "student_id", "question_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quiz_id: Mapped[str] = mapped_column(
        ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("quiz_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )

    selected_answer: Mapped[str | None] = mapped_column(String(500))
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Khan-style retry: a student may re-answer after seeing the explanation.
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    hint_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    feedback_shown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    time_spent_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    quiz: Mapped[Quiz] = relationship(back_populates="responses")
    question: Mapped[QuizQuestion] = relationship(back_populates="responses")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response {self.selected_answer!r} correct={self.is_correct}>"


# --------------------------------------------------------------------------- #
# 8. quiz_attempts
# --------------------------------------------------------------------------- #


class QuizAttempt(Base, TimestampMixin):
    """Immutable history row per finished quiz. Feeds analytics and gap detection.

    Kept separate from ``quizzes`` so that retakes never overwrite the record of
    how a student performed the first time round.
    """

    __tablename__ = "quiz_attempts"
    __table_args__ = (
        Index("ix_attempt_student_subunit", "student_id", "sub_unit_id", "difficulty_level"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null for a cumulative unit test, which spans a whole unit; see Quiz.
    sub_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("curriculum_sub_units.id", ondelete="CASCADE"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("curriculum_units.id", ondelete="CASCADE"), index=True
    )
    quiz_id: Mapped[str] = mapped_column(
        ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    difficulty_level: Mapped[DifficultyLevel] = _enum_column(DifficultyLevel, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    total_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False)
    score_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    is_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_practice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_unit_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # {"add_integers": 0.4, "multiply_fractions": 1.0} - per-skill accuracy.
    skill_breakdown: Mapped[dict | None] = mapped_column(JSON, default=dict)

    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    student: Mapped[Student] = relationship(back_populates="attempts")
    quiz: Mapped[Quiz] = relationship(back_populates="attempt")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attempt #{self.attempt_number} {self.score_percentage}% passed={self.is_passed}>"


# --------------------------------------------------------------------------- #
# 9. sub_unit_progress
# --------------------------------------------------------------------------- #


class SubUnitProgress(Base, TimestampMixin):
    """The 0 / 33 / 67 / 100 % tracker: one row per student per sub-unit.

    This is the single source of truth for unlocking the next unit and for
    deciding whether the celebration animation should fire.
    """

    __tablename__ = "sub_unit_progress"
    __table_args__ = (
        UniqueConstraint("student_id", "sub_unit_id", name="uq_progress_identity"),
        CheckConstraint(
            "completion_percentage IN (0, 33, 67, 100)",
            name="ck_progress_percentage_steps",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sub_unit_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_sub_units.id", ondelete="CASCADE"), nullable=False, index=True
    )

    completion_percentage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[ProgressStatus] = _enum_column(
        ProgressStatus, default=ProgressStatus.NOT_STARTED, nullable=False, index=True
    )

    # One flag + best score per difficulty tier.
    beginner_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    intermediate_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proficient_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    beginner_best_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    intermediate_best_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    proficient_best_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    total_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_time_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Guarantees the 100 % celebration is shown exactly once.
    celebration_shown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    student: Mapped[Student] = relationship(back_populates="progress")
    sub_unit: Mapped[CurriculumSubUnit] = relationship(back_populates="progress")

    # -- domain logic ----------------------------------------------------- #

    def mark_level_complete(self, level: DifficultyLevel, score: float) -> int:
        """Record a passed difficulty level and recompute completion.

        Args:
            level: The difficulty tier that was just passed.
            score: The score achieved, in percent.

        Returns:
            The updated ``completion_percentage`` (0, 33, 67 or 100).
        """
        setattr(self, f"{level.value}_completed", True)
        best_attr = f"{level.value}_best_score"
        if score > getattr(self, best_attr):
            setattr(self, best_attr, score)
        return self.recalculate()

    def record_score(self, level: DifficultyLevel, score: float) -> int:
        """Record any attempt (passed or not) and recompute completion."""
        best_attr = f"{level.value}_best_score"
        if score > getattr(self, best_attr):
            setattr(self, best_attr, score)
        if score >= level.threshold:
            setattr(self, f"{level.value}_completed", True)
        return self.recalculate()

    def recalculate(self) -> int:
        """Recompute percentage and status from the three completion flags."""
        completed = sum(
            (
                self.beginner_completed,
                self.intermediate_completed,
                self.proficient_completed,
            )
        )
        self.completion_percentage = {0: 0, 1: 33, 2: 67, 3: 100}[completed]

        if completed == 3:
            self.status = ProgressStatus.COMPLETED
            if self.completed_at is None:
                self.completed_at = _utcnow()
        elif completed > 0 or self.total_attempts > 0:
            self.status = ProgressStatus.IN_PROGRESS
        else:
            self.status = ProgressStatus.NOT_STARTED

        self.last_activity_at = _utcnow()
        return self.completion_percentage

    @property
    def is_complete(self) -> bool:
        return self.completion_percentage == 100

    @property
    def should_celebrate(self) -> bool:
        """True only on the first render after hitting a genuine 100 %."""
        return self.is_complete and not self.celebration_shown

    @property
    def next_difficulty(self) -> DifficultyLevel | None:
        """The next tier the student has not yet passed, if any."""
        for level in (
            DifficultyLevel.BEGINNER,
            DifficultyLevel.INTERMEDIATE,
            DifficultyLevel.PROFICIENT,
        ):
            if not getattr(self, f"{level.value}_completed"):
                return level
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Progress {self.completion_percentage}% {self.status.value}>"


# --------------------------------------------------------------------------- #
# 10. gamification_profiles
# --------------------------------------------------------------------------- #


class GamificationProfile(Base, TimestampMixin):
    """Points, level, streak and avatar state for one student."""

    __tablename__ = "gamification_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    # Lifetime earnings. Never decreases, because it is what drives the level:
    # a child who saves up and buys a dragon must not drop from level 4 back to
    # level 2 for having used their reward.
    total_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Redeemed in the avatar shop. Spendable balance is the difference.
    points_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    points_to_next_level: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    current_streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activity_date: Mapped[date | None] = mapped_column(Date)

    avatar_key: Mapped[str] = mapped_column(String(60), default="sunny", nullable=False)
    unlocked_avatars: Mapped[list | None] = mapped_column(JSON, default=list)

    total_quizzes_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_correct_answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_sub_units_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_units_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    student: Mapped[Student] = relationship(back_populates="gamification_profile")

    def award_points(self, points: int) -> bool:
        """Add points and level up while the threshold is exceeded.

        Level N requires ``100 * N`` points to clear.

        Returns:
            True if the student gained at least one level.
        """
        self.total_points += points
        levelled_up = False
        while self.total_points >= self.points_to_next_level:
            self.level += 1
            self.points_to_next_level += 100 * self.level
            levelled_up = True
        return levelled_up

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Gamification lvl={self.level} pts={self.total_points}>"


# --------------------------------------------------------------------------- #
# 11. achievement_badges
# --------------------------------------------------------------------------- #


class AchievementBadge(Base, TimestampMixin):
    """An unlocked badge. One row per (student, badge_key)."""

    __tablename__ = "achievement_badges"
    __table_args__ = (UniqueConstraint("student_id", "badge_key", name="uq_badge_per_student"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )

    badge_key: Mapped[str] = mapped_column(String(80), nullable=False)  # "first_100_percent"
    badge_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(80))
    tier: Mapped[str] = mapped_column(String(20), default="bronze", nullable=False)

    points_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # What triggered it: {"sub_unit_id": "...", "score": 95.0}
    context: Mapped[dict | None] = mapped_column(JSON, default=dict)

    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    student: Mapped[Student] = relationship(back_populates="badges")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Badge {self.badge_key} ({self.tier})>"


# --------------------------------------------------------------------------- #
# 12. notifications
# --------------------------------------------------------------------------- #


class Notification(Base, TimestampMixin):
    """An outbound message to a parent/teacher. Queued by APScheduler jobs."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_pending", "is_sent", "scheduled_for"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[str | None] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True
    )

    type: Mapped[NotificationType] = _enum_column(NotificationType, nullable=False, index=True)
    channel: Mapped[NotificationChannel] = _enum_column(
        NotificationChannel, default=NotificationChannel.EMAIL, nullable=False
    )

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="notifications")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification {self.type.value} sent={self.is_sent}>"


# --------------------------------------------------------------------------- #
# 13. sessions
# --------------------------------------------------------------------------- #


class Session(Base, TimestampMixin):
    """A refresh-token session, so that logout can revoke server-side."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_session_user_active", "user_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Only ever store the hash - a leaked DB must not yield usable tokens.
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite hands back naive datetimes
            expires = expires.replace(tzinfo=UTC)
        return expires > _utcnow()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Session user={self.user_id} active={self.is_active}>"


# --------------------------------------------------------------------------- #
# 14. question_bank
# --------------------------------------------------------------------------- #


class QuestionBankItem(Base, TimestampMixin):
    """A pre-generated question, owned by the curriculum rather than a student.

    This is what removes generation from the request path. A background job
    fills the bank ahead of where students are working; ``create_quiz`` then
    copies rows out of it in milliseconds instead of waiting ~2 minutes on the
    model.

    Because items are shared, a defective question here reaches every student
    who draws it -- so bank generation always runs with verification on, and
    anything the verifier disputes is stored inactive for review rather than
    served.

    Retiring is soft (``is_active = False``): student answer history points at
    these rows, and deleting one would erase the record of what was asked.
    """

    __tablename__ = "question_bank"
    __table_args__ = (
        # The same question must not appear twice in one slot.
        UniqueConstraint(
            "sub_unit_id", "difficulty_level", "content_hash", name="uq_bank_question_content"
        ),
        Index("ix_bank_slot", "sub_unit_id", "difficulty_level", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sub_unit_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_sub_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    difficulty_level: Mapped[DifficultyLevel] = _enum_column(DifficultyLevel, nullable=False)

    # --- the question itself, mirroring QuizQuestion ---------------------
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = _enum_column(
        QuestionType, default=QuestionType.MULTIPLE_CHOICE, nullable=False
    )
    options: Mapped[list | None] = mapped_column(JSON, default=list)
    correct_answer: Mapped[str] = mapped_column(String(500), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    distractor_rationales: Mapped[dict | None] = mapped_column(JSON, default=dict)
    hint: Mapped[str | None] = mapped_column(Text)
    skill_tag: Mapped[str | None] = mapped_column(String(120), index=True)
    points: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    source_chunk_ids: Mapped[list | None] = mapped_column(JSON, default=list)

    # --- provenance and QA ------------------------------------------------
    # sha256 of the normalised question text, for slot-level de-duplication.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    generation_metadata: Mapped[dict | None] = mapped_column(JSON, default=dict)

    # "verified" | "unverified" | "disputed"
    verification_status: Mapped[str] = mapped_column(
        String(20), default="unverified", nullable=False, index=True
    )
    verifier_answer: Mapped[str | None] = mapped_column(String(10))
    verification_votes: Mapped[dict | None] = mapped_column(JSON, default=dict)
    review_notes: Mapped[str | None] = mapped_column(Text)

    # --- serving ----------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    times_served: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_served_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sub_unit: Mapped[CurriculumSubUnit] = relationship(back_populates="bank_questions")

    @property
    def is_servable(self) -> bool:
        """True if this item may be handed to a student."""
        return self.is_active and self.verification_status != "disputed"

    def to_question_kwargs(self) -> dict[str, Any]:
        """Fields for copying this item into a student's ``QuizQuestion``."""
        return {
            "question_text": self.question_text,
            "question_type": self.question_type,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "distractor_rationales": self.distractor_rationales,
            "hint": self.hint,
            "difficulty_level": self.difficulty_level,
            "skill_tag": self.skill_tag,
            "source_chunk_ids": self.source_chunk_ids,
            "points": self.points,
            "bank_question_id": self.id,
            # Carried onto the question itself so it stays attributable in a
            # cumulative unit test, where the quiz names no single sub-unit.
            "sub_unit_id": self.sub_unit_id,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<BankQuestion {self.difficulty_level.value} "
            f"{self.verification_status} {self.question_text[:40]!r}>"
        )


# --------------------------------------------------------------------------- #
# 15. curriculum_uploads
# --------------------------------------------------------------------------- #


class UploadStatus(str, enum.Enum):
    """Lifecycle of one curriculum ingestion.

    ``pending -> processing -> review`` reads the PDF and stops. Only once the
    parent confirms does it go ``pending -> processing -> completed`` again,
    this time embedding, indexing and writing the first questions -- the
    expensive part, and the part not worth doing for the wrong file.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    #: Parsed, and waiting for the parent to say it is the right curriculum.
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    #: The parent said it was the wrong file. Nothing was built.
    CANCELLED = "cancelled"


class CurriculumUpload(Base, TimestampMixin):
    """One attempt at ingesting a curriculum PDF.

    Ingestion takes minutes -- parse, chunk, embed, upsert -- so it cannot
    happen inside the request that uploads the file. This row is what the
    parent polls, and it is a durable row rather than in-memory state for two
    reasons: a restart mid-ingest would otherwise leave them staring at a
    spinner with nothing behind it, and "what did I upload, and did it work?"
    is a question worth being able to answer tomorrow.
    """

    __tablename__ = "curriculum_uploads"
    __table_args__ = (Index("ix_upload_user_status", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    subject: Mapped[str] = mapped_column(String(80), default="math", nullable=False)
    grade_level: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[UploadStatus] = _enum_column(
        UploadStatus, default=UploadStatus.PENDING, nullable=False, index=True
    )
    # Free text for the parent, not a stack trace: "we could not find any units
    # in this PDF" is actionable, an exception class is not.
    error: Mapped[str | None] = mapped_column(Text)

    units_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sub_units_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vectors_upserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warnings: Mapped[list | None] = mapped_column(JSON, default=list)
    # What the parser found -- title, grade, units -- shown to the parent
    # for confirmation before anything is embedded or generated.
    preview: Mapped[dict | None] = mapped_column(JSON)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="curriculum_uploads")

    @property
    def is_finished(self) -> bool:
        return self.status in (UploadStatus.COMPLETED, UploadStatus.FAILED)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CurriculumUpload {self.filename!r} {self.status.value}>"


# --------------------------------------------------------------------------- #
# Construction-time defaults
# --------------------------------------------------------------------------- #


@event.listens_for(Base, "init", propagate=True)
def _apply_scalar_defaults(target: object, _args: tuple, kwargs: dict) -> None:
    """Apply scalar column defaults when the object is constructed.

    SQLAlchemy's ``default=`` is an *INSERT-time* default: until a flush
    happens, ``GamificationProfile().total_points`` is ``None``, not ``0``.
    The domain helpers on these models (``award_points``, ``record_score``,
    ``recalculate``) run on freshly constructed, unflushed objects, so they
    would otherwise hit ``None + int``.

    This listener copies literal defaults onto the instance at construction so
    in-memory objects behave the same as loaded ones. Callable defaults
    (``_uuid``, ``_utcnow``, ``list``) are deliberately left to flush time --
    they are not needed before persistence and evaluating them early would
    stamp timestamps too soon.
    """
    mapper = sa_inspect(type(target)).mapper
    for attr in mapper.column_attrs:
        if attr.key in kwargs:
            continue
        column = attr.columns[0]
        default = column.default
        if default is not None and default.is_scalar:
            setattr(target, attr.key, default.arg)


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #

__all__ = [
    "Base",
    # enums
    "UserRole",
    "DifficultyLevel",
    "QuizStatus",
    "ProgressStatus",
    "NotificationType",
    "NotificationChannel",
    "QuestionType",
    "UploadStatus",
    # tables
    "User",
    "Student",
    "CurriculumUnit",
    "CurriculumSubUnit",
    "Quiz",
    "QuizQuestion",
    "QuizResponse",
    "QuizAttempt",
    "SubUnitProgress",
    "GamificationProfile",
    "AchievementBadge",
    "Notification",
    "Session",
    "QuestionBankItem",
    "CurriculumUpload",
]
