"""Gamification routes: points, level, streak, badges and avatar.

Read-mostly. Points and badges are awarded by
``services.gamification.award_for_attempt`` when a quiz is completed, never by
a client call -- an endpoint that grants points on request is an endpoint that
grants points on request.

There is deliberately no leaderboard. Ranking children against each other
turns a personal mastery signal into a comparison a child can lose, and the
progression here is explicitly about your own 0/33/67/100 rather than someone
else's.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_owned_student
from api.schemas import (
    AvatarOut,
    AvatarSelectRequest,
    AvatarShopOut,
    BadgeOut,
    EarningRuleOut,
    GamificationProfileOut,
    PointsGuideOut,
)
from db.database import get_db
from db.models import AchievementBadge, Student, User
from services import avatars
from services.avatars import AvatarError
from services.gamification import (
    BADGE_RULES,
    buy_avatar,
    earning_rules,
    get_or_create_profile,
    wear_avatar,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gamification", tags=["gamification"])


def _level_progress(total_points: int, points_to_next_level: int, level: int) -> float:
    """How far through the current level the student is, 0.0 to 1.0.

    Levels get progressively longer, so the bar has to measure the span of the
    *current* level rather than progress towards a moving total.
    """
    # points_to_next_level is cumulative; the previous threshold is it minus
    # this level's own span, which is 100 * level.
    span = 100 * level
    floor = max(0, points_to_next_level - span)
    if points_to_next_level <= floor:
        return 1.0
    fraction = (total_points - floor) / (points_to_next_level - floor)
    return round(min(1.0, max(0.0, fraction)), 3)


def _profile_view(db: Session, student: Student) -> GamificationProfileOut:
    """Serialise a student's gamification profile.

    Shared by every route that returns one, so buying an avatar and reading the
    dashboard cannot disagree about the balance.
    """
    profile = get_or_create_profile(db, student.id)
    earned = db.query(AchievementBadge).filter_by(student_id=student.id).count()
    worn = avatars.get(profile.avatar_key)

    return GamificationProfileOut(
        student_id=student.id,
        student_name=student.display_name,
        total_points=profile.total_points,
        points_spent=profile.points_spent,
        points_balance=avatars.balance(profile.total_points, profile.points_spent),
        level=profile.level,
        points_to_next_level=profile.points_to_next_level,
        level_progress=_level_progress(
            profile.total_points, profile.points_to_next_level, profile.level
        ),
        current_streak_days=profile.current_streak_days,
        longest_streak_days=profile.longest_streak_days,
        last_activity_date=profile.last_activity_date,
        # The current key, even for a profile saved under a first-catalogue one.
        avatar_key=worn.key if worn else profile.avatar_key,
        avatar_image=worn.image if worn else "",
        avatar_name=worn.name if worn else "",
        unlocked_avatars=sorted(avatars.owned(profile.unlocked_avatars)),
        total_quizzes_completed=profile.total_quizzes_completed,
        total_correct_answers=profile.total_correct_answers,
        total_sub_units_completed=profile.total_sub_units_completed,
        total_units_completed=profile.total_units_completed,
        badges_earned=earned,
        badges_total=len(BADGE_RULES),
    )


@router.get("/students/{student_id}", response_model=GamificationProfileOut)
def read_profile(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> GamificationProfileOut:
    """Points, level, streak and avatar for one student."""
    view = _profile_view(db, student)
    db.commit()
    return view


@router.get("/students/{student_id}/badges", response_model=list[BadgeOut])
def list_badges(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
    earned_only: bool = Query(default=False, description="Hide badges that are still locked"),
) -> list[BadgeOut]:
    """Every badge, earned first, then the ones still to win.

    Locked badges are shown by default. A child seeing what is still available
    is the point of having them; a list that only shows what you already have
    motivates nobody.
    """
    earned = {
        badge.badge_key: badge
        for badge in db.query(AchievementBadge).filter_by(student_id=student.id).all()
    }

    results: list[BadgeOut] = []
    for rule in BADGE_RULES:
        badge = earned.get(rule.key)
        if badge is None and earned_only:
            continue
        results.append(
            BadgeOut(
                badge_key=rule.key,
                badge_name=rule.name,
                description=rule.description,
                icon=rule.icon,
                tier=rule.tier,
                points_awarded=rule.points,
                earned=badge is not None,
                unlocked_at=badge.unlocked_at if badge else None,
            )
        )

    # Earned first, newest first within that; then locked, cheapest first so
    # the nearest goal is at the top.
    results.sort(
        key=lambda b: (
            not b.earned,
            -(b.unlocked_at.timestamp() if b.unlocked_at else 0),
            b.points_awarded,
        )
    )
    return results


@router.post("/students/{student_id}/avatar", response_model=GamificationProfileOut)
def select_avatar(
    payload: AvatarSelectRequest,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> GamificationProfileOut:
    """Wear an avatar the student already owns.

    Selecting one they have not bought is refused: an avatar that can be set by
    asking is not a reward. Ownership is decided in ``services.avatars`` rather
    than here, so buying and wearing cannot disagree about what is owned.
    """
    try:
        wear_avatar(db, student.id, payload.avatar_key)
    except AvatarError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    view = _profile_view(db, student)
    db.commit()
    logger.info("Student %s is wearing %s", student.id[:8], payload.avatar_key)
    return view


@router.get("/points-guide", response_model=PointsGuideOut)
def read_points_guide() -> PointsGuideOut:
    """How points are earned and what they buy.

    Unauthenticated on purpose: it is the same for everybody, contains nothing
    about any child, and the sign-up screen wants to be able to say what the
    app rewards before anyone has an account.
    """
    for_sale = sorted(
        (avatar for avatar in avatars.CATALOGUE if not avatar.is_starter),
        key=lambda avatar: avatar.price,
    )
    return PointsGuideOut(
        earning=[EarningRuleOut(**rule) for rule in earning_rules()],
        quiz_worth=avatars.QUIZ_WORTH,
        cheapest_avatar_price=for_sale[0].price if for_sale else 0,
        spend_on="new characters to be",
    )


@router.get("/avatars/starters", response_model=list[AvatarOut])
def list_starters(user: User = Depends(get_current_user)) -> list[AvatarOut]:
    """The free starter characters, without needing a student.

    A student-scoped shop cannot answer this: the very first child has to pick
    a character *before* they exist, and asking for one child's shop to set up
    another child's signup would be a lie about whose ownership is being read.

    Nothing here is per-child, so no ownership is reported -- starters are free
    and every child has all of them.
    """
    return [
        AvatarOut(
            key=avatar.key,
            name=avatar.name,
            image=avatar.image,
            price=avatar.price,
            blurb=avatar.blurb,
            is_starter=True,
            owned=True,
            affordable=True,
        )
        for avatar in avatars.STARTERS
    ]


@router.get("/students/{student_id}/avatars", response_model=AvatarShopOut)
def read_avatar_shop(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> AvatarShopOut:
    """Every character, with what this child owns and can afford.

    Locked characters are returned too, priced. A shop that hides what you
    cannot yet afford gives a child nothing to save towards.
    """
    profile = get_or_create_profile(db, student.id)
    db.commit()
    worn = avatars.get(profile.avatar_key)
    return AvatarShopOut(
        points_balance=avatars.balance(profile.total_points, profile.points_spent),
        total_points=profile.total_points,
        points_spent=profile.points_spent,
        wearing=worn.key if worn else profile.avatar_key,
        avatars=[
            AvatarOut(
                **avatars.to_dict(
                    avatar,
                    profile.unlocked_avatars,
                    profile.total_points,
                    profile.points_spent,
                )
            )
            for avatar in avatars.CATALOGUE
        ],
    )


@router.post(
    "/students/{student_id}/avatars/{avatar_key}/buy",
    response_model=GamificationProfileOut,
)
def buy_avatar_route(
    avatar_key: str,
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> GamificationProfileOut:
    """Spend points on a character, and wear it.

    The only endpoint that moves points, and it only ever moves them *out*.
    Earning stays where it belongs: awarded by completing a quiz, never by
    asking.
    """
    try:
        buy_avatar(db, student.id, avatar_key)
    except AvatarError as exc:
        # 409, not 422: the request is well formed, the child just cannot
        # afford it yet -- and the message is written for them to read.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    db.commit()
    return _profile_view(db, student)
