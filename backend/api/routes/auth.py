"""Authentication routes: signup, login, refresh, logout, student profiles.

Refresh tokens are recorded (hashed) in the ``sessions`` table so logout can
actually revoke them. Refreshing rotates the token and revokes the old one:
if a stolen refresh token is used, the legitimate user's next refresh fails
and the theft surfaces instead of granting quiet 30-day access.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_owned_student
from api.schemas import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    StudentCreate,
    StudentOut,
    TokenPair,
    UserOut,
)
from config import settings
from db.database import get_db
from db.models import GamificationProfile, Student, User, UserRole
from db.models import Session as UserSession
from services import avatars
from utils.security import (
    AuthError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(db: Session, user: User, request: Request | None = None) -> TokenPair:
    """Mint an access/refresh pair and record the refresh session."""
    access = create_access_token(user.id)
    refresh = create_refresh_token(user.id)

    db.add(
        UserSession(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh),
            user_agent=(request.headers.get("user-agent") if request else None),
            ip_address=(request.client.host if request and request.client else None),
            expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    db.flush()

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """Create an account and return a token pair."""
    email = payload.email.lower().strip()
    if db.query(User).filter_by(email=email).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )

    try:
        hashed = hash_password(payload.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    user = User(
        email=email,
        hashed_password=hashed,
        full_name=payload.full_name.strip(),
        role=UserRole(payload.role),
        timezone=payload.timezone,
    )
    db.add(user)
    db.flush()

    tokens = _issue_tokens(db, user, request)
    db.commit()
    logger.info("New %s account: %s", user.role.value, user.email)
    return tokens


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """Exchange credentials for a token pair."""
    email = payload.email.lower().strip()
    user = db.query(User).filter_by(email=email).first()

    # Verify against a dummy hash when the user is absent, so a missing account
    # and a wrong password take the same time and cannot be told apart.
    if user is None:
        verify_password(payload.password, "$2b$12$" + "x" * 53)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is deactivated."
        )

    user.last_login_at = datetime.now(UTC)
    tokens = _issue_tokens(db, user, request)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    """Exchange a refresh token for a new pair, rotating the old one."""
    try:
        claims = decode_token(payload.refresh_token, expect="refresh")
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token."
        ) from exc

    token_hash = hash_refresh_token(payload.refresh_token)
    session = db.query(UserSession).filter_by(refresh_token_hash=token_hash).first()

    if session is None or not session.is_active:
        # Signature was valid but the session is revoked or expired -- a
        # logged-out or already-rotated token.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This session is no longer valid. Please sign in again.",
        )

    user = db.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable.")

    # Rotate: the presented token cannot be replayed.
    session.revoked_at = datetime.now(UTC)
    session.last_used_at = datetime.now(UTC)

    tokens = _issue_tokens(db, user, request)
    db.commit()
    return tokens


# response_model=None is required, not decorative: this module uses
# `from __future__ import annotations`, so `-> None` reaches FastAPI as the
# string "None", resolves to NoneType, and NoneType is truthy -- FastAPI
# then believes there is a response body and refuses to mount a 204.
@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> None:
    """Revoke one refresh session.

    Always returns 204, even for an unknown token: a caller must not be able
    to probe which refresh tokens are live.
    """
    session = (
        db.query(UserSession)
        .filter_by(refresh_token_hash=hash_refresh_token(payload.refresh_token))
        .first()
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)
        db.commit()


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """Revoke every session for the caller, on every device."""
    now = datetime.now(UTC)
    revoked = 0
    for session in db.query(UserSession).filter_by(user_id=user.id).all():
        if session.revoked_at is None:
            session.revoked_at = now
            revoked += 1
    db.commit()
    logger.info("Revoked %d session(s) for %s", revoked, user.email)


@router.get("/me", response_model=UserOut)
def read_me(user: User = Depends(get_current_user)) -> User:
    """Return the authenticated account."""
    return user


# --------------------------------------------------------------------------- #
# Student profiles
# --------------------------------------------------------------------------- #


@router.post("/students", response_model=StudentOut, status_code=status.HTTP_201_CREATED)
def create_student(
    payload: StudentCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Student:
    """Add a child profile under the caller's account."""
    student = Student(
        user_id=user.id,
        first_name=payload.first_name.strip(),
        last_name=(payload.last_name or "").strip() or None,
        grade_level=payload.grade_level,
        date_of_birth=payload.date_of_birth,
        school_name=payload.school_name,
    )
    db.add(student)
    db.flush()

    # Every student needs a gamification profile from the start; creating it
    # lazily would mean the first points award has to handle a missing row.
    # Only starters are accepted here: a paid avatar chosen at signup would be
    # one nobody paid for.
    chosen = avatars.get(payload.avatar_key or "")
    db.add(
        GamificationProfile(
            student_id=student.id,
            avatar_key=(
                chosen.key if chosen is not None and chosen.is_starter else avatars.DEFAULT_AVATAR
            ),
            unlocked_avatars=avatars.starter_keys(),
        )
    )
    db.commit()

    logger.info("Created student %s for %s", student.display_name, user.email)
    return student


@router.get("/students", response_model=list[StudentOut])
def list_students(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Student]:
    """List the caller's active student profiles."""
    return (
        db.query(Student)
        .filter_by(user_id=user.id, is_active=True)
        .order_by(Student.created_at)
        .all()
    )


@router.get("/students/{student_id}", response_model=StudentOut)
def read_student(student: Student = Depends(get_owned_student)) -> Student:
    """Return one of the caller's students."""
    return student


@router.delete("/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student(
    student: Student = Depends(get_owned_student),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a child from the account, with everything that is theirs.

    Their quizzes, progress, points, badges and characters go with them. A
    parent who removes a child expects the child gone, not hidden with their
    work still on file -- so this deletes rather than deactivates, and the
    screen that calls it says so before it is done.
    """
    name = student.display_name
    db.delete(student)
    db.commit()
    logger.info("Removed student %s", name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
