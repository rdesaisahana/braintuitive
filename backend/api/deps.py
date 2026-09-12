"""Shared FastAPI dependencies: authentication and ownership.

``get_owned_student`` is the important one. Every quiz and progress route is
addressed by ``student_id``, and without an ownership check any authenticated
parent could read or alter another family's child by guessing an id. The rule
is enforced here, once, rather than repeated in each handler where it could be
forgotten.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import Student, User, UserRole
from utils.security import AuthError, decode_token

logger = logging.getLogger(__name__)

# auto_error=False so a missing header yields our own 401 with a useful
# message rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the caller from their access token.

    Raises:
        HTTPException: 401 if the token is missing, invalid, expired or the
            wrong type; 403 if the account has been deactivated.
    """
    if credentials is None or not credentials.credentials:
        raise CREDENTIALS_EXCEPTION

    try:
        claims = decode_token(credentials.credentials, expect="access")
    except AuthError as exc:
        logger.debug("Rejected access token: %s", exc)
        raise CREDENTIALS_EXCEPTION from exc

    user = db.get(User, claims["sub"])
    if user is None:
        # The token verified but the account is gone.
        raise CREDENTIALS_EXCEPTION
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is deactivated."
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Restrict a route to administrators."""
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required."
        )
    return user


def get_owned_student(
    student_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Student:
    """Load a student, confirming the caller owns them.

    A student that exists but belongs to someone else returns 404, not 403.
    Distinguishing the two would confirm the id is real, letting an attacker
    enumerate which children exist on the platform.
    """
    student = db.get(Student, student_id)
    if student is None or (student.user_id != user.id and user.role is not UserRole.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found.")
    if not student.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This student profile is inactive."
        )
    return student


def assert_owns_student(db: Session, user: User, student_id: str) -> Student:
    """Ownership check for handlers that take ``student_id`` in the body.

    ``get_owned_student`` only works when the id is a path parameter.
    """
    student = db.get(Student, student_id)
    if student is None or (student.user_id != user.id and user.role is not UserRole.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found.")
    return student


__all__ = [
    "assert_owns_student",
    "bearer_scheme",
    "get_current_user",
    "get_owned_student",
    "require_admin",
]
