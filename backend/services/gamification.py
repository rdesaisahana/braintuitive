"""Points, levels, streaks and badges.

Nothing wrote to ``gamification_profiles`` before this module: a profile was
created with each student and then left untouched. This is the engine that
makes it mean something.

Two rules shape the design:

**Practice earns nothing.** A retry of a tier the child has already passed is
deliberately low-stakes -- they can attempt it as often as they like. If it
also paid points, the entire economy could be farmed by replaying one easy
sub-unit, and a level would stop saying anything about learning. Practice
still costs nothing; it simply also earns nothing.

**A badge is awarded once, ever.** ``achievement_badges`` has a unique
constraint on ``(student_id, badge_key)``, and every rule here is checked
against what the student has already earned. A celebration that repeats is not
a celebration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from db.models import (
    AchievementBadge,
    CurriculumSubUnit,
    DifficultyLevel,
    GamificationProfile,
    QuizAttempt,
    Student,
    SubUnitProgress,
)
from services import avatars
from services.curriculum_scope import visible_units

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Points
# --------------------------------------------------------------------------- #

POINTS_PER_CORRECT_ANSWER = 10
POINTS_FOR_PASSING = 25
POINTS_FOR_PERFECT_SCORE = 25
# Awarded the first time a difficulty tier is cleared on a sub-unit.
POINTS_FOR_TIER_COMPLETE = 50
POINTS_FOR_SUB_UNIT_COMPLETE = 100
POINTS_FOR_UNIT_COMPLETE = 250


def earning_rules() -> list[dict[str, object]]:
    """How points are earned, in the order a child meets them.

    Derived from the constants above rather than written out again, because a
    child who is told "10 points per answer" and paid 5 has been lied to -- and
    the way that happens is a number typed into an interface drifting from the
    number the engine actually uses.

    Wording is aimed at the child, since they are the one reading it.
    """
    return [
        {
            "key": "correct_answer",
            "points": POINTS_PER_CORRECT_ANSWER,
            "label": "Every question you get right",
            "detail": "Answer well and it adds up fast.",
        },
        {
            "key": "passing",
            "points": POINTS_FOR_PASSING,
            "label": "Passing a quiz",
            "detail": "Reach the score you need and this is yours.",
        },
        {
            "key": "perfect_score",
            "points": POINTS_FOR_PERFECT_SCORE,
            "label": "Getting every single one right",
            "detail": "A perfect score pays a bonus on top.",
        },
        {
            "key": "tier_complete",
            "points": POINTS_FOR_TIER_COMPLETE,
            "label": "Finishing a level",
            "detail": "Beginner, then intermediate, then proficient.",
        },
        {
            "key": "sub_unit_complete",
            "points": POINTS_FOR_SUB_UNIT_COMPLETE,
            "label": "Finishing a whole topic",
            "detail": "All three levels of one topic done.",
        },
        {
            "key": "unit_complete",
            "points": POINTS_FOR_UNIT_COMPLETE,
            "label": "Finishing a whole unit",
            "detail": "The big one. Every topic in the unit, complete.",
        },
    ]


# Avatars are bought, not granted -- see services.avatars for the catalogue
# and prices. Levelling still happens and still means something, but it is a
# record of how far a child has come rather than the gate on what they can be.


@dataclass
class AwardReport:
    """What one completed attempt earned."""

    points_earned: int = 0
    total_points: int = 0
    level: int = 1
    levelled_up: bool = False
    points_to_next_level: int = 0
    current_streak_days: int = 0
    streak_extended: bool = False
    new_badges: list[AchievementBadge] = field(default_factory=list)
    # Avatars the child can now afford -- not ones they have been given.
    avatars_unlocked: list[str] = field(default_factory=list)
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def earned_anything(self) -> bool:
        return bool(self.points_earned or self.new_badges or self.avatars_unlocked)


# --------------------------------------------------------------------------- #
# Badges
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BadgeRule:
    """One badge and the condition that unlocks it."""

    key: str
    name: str
    description: str
    icon: str
    tier: str
    points: int
    # Given the attempt and the surrounding state, has this been earned?
    predicate: Callable[[BadgeContext], bool]


@dataclass
class BadgeContext:
    """Everything a badge rule may look at."""

    attempt: QuizAttempt
    profile: GamificationProfile
    progress: SubUnitProgress | None
    sub_unit_completed_now: bool
    unit_completed_now: bool
    hints_used: int
    total_graded_attempts: int
    previously_failed_this_tier: bool


BADGE_RULES: tuple[BadgeRule, ...] = (
    BadgeRule(
        key="first_quiz",
        name="First Steps",
        description="Finished your very first quiz.",
        icon="footprints",
        tier="bronze",
        points=25,
        predicate=lambda c: c.total_graded_attempts == 1,
    ),
    BadgeRule(
        key="first_pass",
        name="On the Board",
        description="Passed a quiz for the first time.",
        icon="check_circle",
        tier="bronze",
        points=25,
        predicate=lambda c: c.attempt.is_passed,
    ),
    BadgeRule(
        key="perfect_score",
        name="Flawless",
        description="Answered every question in a quiz correctly.",
        icon="star",
        tier="silver",
        points=50,
        predicate=lambda c: c.attempt.score_percentage >= 100.0,
    ),
    BadgeRule(
        key="unaided_perfect",
        name="No Help Needed",
        description="A perfect score without using a single hint.",
        icon="brain",
        tier="gold",
        points=75,
        predicate=lambda c: c.attempt.score_percentage >= 100.0 and c.hints_used == 0,
    ),
    BadgeRule(
        key="comeback",
        name="Second Wind",
        description="Passed a tier you had failed before. Persistence pays.",
        icon="refresh",
        tier="silver",
        points=50,
        predicate=lambda c: c.attempt.is_passed and c.previously_failed_this_tier,
    ),
    BadgeRule(
        key="proficient_pass",
        name="Deep Thinker",
        description="Cleared a proficient quiz -- the hardest tier there is.",
        icon="lightbulb",
        tier="gold",
        points=75,
        predicate=lambda c: (
            c.attempt.is_passed and c.attempt.difficulty_level is DifficultyLevel.PROFICIENT
        ),
    ),
    BadgeRule(
        key="sub_unit_master",
        name="Sub-Unit Master",
        description="Took a sub-unit all the way to 100%.",
        icon="trophy",
        tier="gold",
        points=100,
        predicate=lambda c: c.sub_unit_completed_now,
    ),
    BadgeRule(
        key="unit_master",
        name="Unit Champion",
        description="Completed an entire unit.",
        icon="crown",
        tier="platinum",
        points=250,
        predicate=lambda c: c.unit_completed_now,
    ),
    BadgeRule(
        key="streak_3",
        name="Three in a Row",
        description="Practised three days running.",
        icon="flame",
        tier="bronze",
        points=30,
        predicate=lambda c: c.profile.current_streak_days >= 3,
    ),
    BadgeRule(
        key="streak_7",
        name="Week Strong",
        description="Seven days in a row. That is a habit.",
        icon="flame",
        tier="silver",
        points=75,
        predicate=lambda c: c.profile.current_streak_days >= 7,
    ),
    BadgeRule(
        key="streak_30",
        name="Unstoppable",
        description="Thirty days in a row.",
        icon="flame",
        tier="platinum",
        points=300,
        predicate=lambda c: c.profile.current_streak_days >= 30,
    ),
)

BADGES_BY_KEY: dict[str, BadgeRule] = {rule.key: rule for rule in BADGE_RULES}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def get_or_create_profile(db: Session, student_id: str) -> GamificationProfile:
    """Fetch a student's profile, creating it if it is somehow missing.

    A new profile owns the starter avatars outright. ``services.avatars.owned``
    adds them to whatever is stored, so this is belt and braces -- but it means
    the row on disk reflects what the child actually has rather than relying on
    every reader to remember.
    """
    profile = db.query(GamificationProfile).filter_by(student_id=student_id).one_or_none()
    if profile is None:
        profile = GamificationProfile(
            student_id=student_id,
            unlocked_avatars=avatars.starter_keys(),
            avatar_key=avatars.DEFAULT_AVATAR,
        )
        db.add(profile)
        db.flush()
    return profile


def buy_avatar(db: Session, student_id: str, key: str) -> GamificationProfile:
    """Spend points on an avatar and wear it.

    Wearing it immediately is deliberate: a child who just spent 800 points
    wants to see the otter, not to be returned to a grid and asked to pick
    again.

    Raises:
        avatars.AvatarError: unknown, already owned, or not enough points.
    """
    profile = get_or_create_profile(db, student_id)
    unlocked, spent = avatars.purchase(
        key, profile.unlocked_avatars, profile.total_points, profile.points_spent
    )
    profile.unlocked_avatars = unlocked
    profile.points_spent = spent
    profile.avatar_key = key
    db.flush()
    logger.info(
        "Student %s bought %s for %d point(s); balance now %d.",
        student_id[:8],
        key,
        avatars.get(key).price if avatars.get(key) else 0,
        avatars.balance(profile.total_points, profile.points_spent),
    )
    return profile


def wear_avatar(db: Session, student_id: str, key: str) -> GamificationProfile:
    """Switch to an avatar the child already owns.

    Raises:
        avatars.AvatarError: unknown, or not owned yet.
    """
    profile = get_or_create_profile(db, student_id)
    profile.avatar_key = avatars.select(key, profile.unlocked_avatars)
    db.flush()
    return profile


def update_streak(profile: GamificationProfile, today: date | None = None) -> bool:
    """Advance the daily streak. Returns True if it grew.

    Yesterday continues the streak, today is a no-op, and anything older
    restarts at one. A child who works twice in a day should not get two
    days' credit.
    """
    today = today or datetime.now(UTC).date()
    last = profile.last_activity_date

    if last == today:
        return False
    if last == today - timedelta(days=1):
        profile.current_streak_days += 1
    else:
        profile.current_streak_days = 1

    profile.last_activity_date = today
    profile.longest_streak_days = max(profile.longest_streak_days or 0, profile.current_streak_days)
    return True


def affordable_now(profile: GamificationProfile) -> list[str]:
    """Avatars this child could buy right now, cheapest first.

    Reported after a quiz so the app can say "you can afford Rusty" at the
    moment the points land, which is when it means something. Nothing is
    granted here -- buying stays an explicit choice, because a reward chosen
    is worth more than a reward received.
    """
    have = avatars.owned(profile.unlocked_avatars)
    return [
        avatar.key
        for avatar in sorted(avatars.CATALOGUE, key=lambda item: item.price)
        if avatar.key not in have
        and avatars.can_afford(avatar, profile.total_points, profile.points_spent)
    ]


def _sub_unit_just_completed(progress: SubUnitProgress | None) -> bool:
    """True when this attempt took the sub-unit to 100%."""
    return bool(progress and progress.completion_percentage == 100)


def _unit_just_completed(db: Session, student_id: str, sub_unit_id: str | None) -> bool:
    """True when every sub-unit of this sub-unit's unit is now at 100%.

    ``sub_unit_id`` is None for a cumulative unit test, which moves no
    progress and so can never be the attempt that completes a unit.
    """
    if sub_unit_id is None:
        return False
    sub_unit = db.get(CurriculumSubUnit, sub_unit_id)
    if sub_unit is None:
        return False

    siblings = [
        row.id
        for row in db.query(CurriculumSubUnit).filter_by(unit_id=sub_unit.unit_id, is_active=True)
    ]
    if not siblings:
        return False

    completed = (
        db.query(SubUnitProgress)
        .filter(
            SubUnitProgress.student_id == student_id,
            SubUnitProgress.sub_unit_id.in_(siblings),
            SubUnitProgress.completion_percentage == 100,
        )
        .count()
    )
    return completed == len(siblings)


def _count_completed_units(db: Session, student_id: str, grade_level: int) -> int:
    """How many units the student has finished entirely.

    Counts only the curriculum this child actually works from. Counting every
    grade-6 unit in the database would mean a second family's upload silently
    lowered this child's completion.
    """
    student = db.get(Student, student_id)
    units = visible_units(db, student) if student is not None else []
    total = 0
    for unit in units:
        siblings = [
            row.id for row in db.query(CurriculumSubUnit).filter_by(unit_id=unit.id, is_active=True)
        ]
        if not siblings:
            continue
        done = (
            db.query(SubUnitProgress)
            .filter(
                SubUnitProgress.student_id == student_id,
                SubUnitProgress.sub_unit_id.in_(siblings),
                SubUnitProgress.completion_percentage == 100,
            )
            .count()
        )
        if done == len(siblings):
            total += 1
    return total


def award_for_attempt(
    db: Session,
    attempt: QuizAttempt,
    progress: SubUnitProgress | None = None,
    hints_used: int = 0,
) -> AwardReport:
    """Award points, advance the streak and unlock badges for one attempt.

    Practice attempts earn nothing at all -- see the module docstring. The
    report is still returned so a caller can show "practice, no points" rather
    than silently nothing.
    """
    profile = get_or_create_profile(db, attempt.student_id)
    report = AwardReport(
        total_points=profile.total_points,
        level=profile.level,
        points_to_next_level=profile.points_to_next_level,
        current_streak_days=profile.current_streak_days,
    )

    if attempt.is_practice:
        logger.debug("Practice attempt %s earns no points.", attempt.id[:8])
        return report

    # --- lifetime counters ------------------------------------------------
    profile.total_quizzes_completed += 1
    profile.total_correct_answers += attempt.correct_count

    report.streak_extended = update_streak(profile)
    report.current_streak_days = profile.current_streak_days

    # --- points -----------------------------------------------------------
    breakdown: dict[str, int] = {
        "correct_answers": attempt.correct_count * POINTS_PER_CORRECT_ANSWER
    }
    if attempt.is_passed:
        breakdown["passed"] = POINTS_FOR_PASSING
    if attempt.score_percentage >= 100.0:
        breakdown["perfect_score"] = POINTS_FOR_PERFECT_SCORE

    # A unit test earns points for the work done, but never the completion
    # bonuses: those are paid for clearing a tier or a sub-unit, and a
    # cumulative paper moves no progress, so there is nothing to have cleared.
    tier_done = not attempt.is_unit_test and bool(
        progress and getattr(progress, f"{attempt.difficulty_level.value}_completed", False)
    )
    # Only the attempt that first clears a tier pays the tier bonus; later
    # passes of the same tier would otherwise pay it again.
    if tier_done and attempt.is_passed and attempt.attempt_number >= 1:
        already_paid = (
            db.query(QuizAttempt)
            .filter(
                QuizAttempt.student_id == attempt.student_id,
                QuizAttempt.sub_unit_id == attempt.sub_unit_id,
                QuizAttempt.difficulty_level == attempt.difficulty_level,
                QuizAttempt.is_passed.is_(True),
                QuizAttempt.is_practice.is_(False),
                QuizAttempt.id != attempt.id,
            )
            .count()
        )
        if already_paid == 0:
            breakdown["tier_complete"] = POINTS_FOR_TIER_COMPLETE

    sub_unit_done = _sub_unit_just_completed(progress)
    if sub_unit_done and progress is not None and not progress.celebration_shown:
        breakdown["sub_unit_complete"] = POINTS_FOR_SUB_UNIT_COMPLETE

    unit_done = _unit_just_completed(db, attempt.student_id, attempt.sub_unit_id)
    student = profile.student
    completed_units = (
        _count_completed_units(db, attempt.student_id, student.grade_level) if student else 0
    )
    unit_newly_done = unit_done and completed_units > (profile.total_units_completed or 0)
    if unit_newly_done:
        breakdown["unit_complete"] = POINTS_FOR_UNIT_COMPLETE

    # --- badges -----------------------------------------------------------
    context = BadgeContext(
        attempt=attempt,
        profile=profile,
        progress=progress,
        sub_unit_completed_now=sub_unit_done,
        unit_completed_now=unit_newly_done,
        hints_used=hints_used,
        total_graded_attempts=(
            db.query(QuizAttempt)
            .filter_by(student_id=attempt.student_id, is_practice=False)
            .count()
        ),
        previously_failed_this_tier=(
            db.query(QuizAttempt)
            .filter(
                QuizAttempt.student_id == attempt.student_id,
                QuizAttempt.sub_unit_id == attempt.sub_unit_id,
                QuizAttempt.difficulty_level == attempt.difficulty_level,
                QuizAttempt.is_passed.is_(False),
                QuizAttempt.id != attempt.id,
            )
            .count()
            > 0
        ),
    )

    already_earned = {
        row.badge_key
        for row in db.query(AchievementBadge.badge_key).filter_by(student_id=attempt.student_id)
    }
    for rule in BADGE_RULES:
        if rule.key in already_earned:
            continue
        try:
            if not rule.predicate(context):
                continue
        except Exception:  # pragma: no cover - a bad rule must not lose a quiz
            logger.exception("Badge rule %s raised; skipping.", rule.key)
            continue

        badge = AchievementBadge(
            student_id=attempt.student_id,
            badge_key=rule.key,
            badge_name=rule.name,
            description=rule.description,
            icon=rule.icon,
            tier=rule.tier,
            points_awarded=rule.points,
            context={
                "attempt_id": attempt.id,
                "sub_unit_id": attempt.sub_unit_id,
                "difficulty": attempt.difficulty_level.value,
                "score": attempt.score_percentage,
            },
        )
        db.add(badge)
        report.new_badges.append(badge)
        breakdown[f"badge:{rule.key}"] = rule.points

    # --- apply ------------------------------------------------------------
    total = sum(breakdown.values())
    report.breakdown = breakdown
    report.points_earned = total
    report.levelled_up = profile.award_points(total) if total else False

    profile.total_sub_units_completed = (
        db.query(SubUnitProgress)
        .filter_by(student_id=attempt.student_id, completion_percentage=100)
        .count()
    )
    profile.total_units_completed = completed_units

    report.avatars_unlocked = affordable_now(profile)
    report.total_points = profile.total_points
    report.level = profile.level
    report.points_to_next_level = profile.points_to_next_level

    db.flush()
    logger.info(
        "Awarded %d point(s) to student %s (level %d%s), %d new badge(s).",
        total,
        attempt.student_id[:8],
        profile.level,
        " - LEVEL UP" if report.levelled_up else "",
        len(report.new_badges),
    )
    return report


__all__ = [
    "affordable_now",
    "buy_avatar",
    "wear_avatar",
    "BADGES_BY_KEY",
    "BADGE_RULES",
    "AwardReport",
    "BadgeContext",
    "BadgeRule",
    "award_for_attempt",
    "earning_rules",
    "get_or_create_profile",
    "update_streak",
]
