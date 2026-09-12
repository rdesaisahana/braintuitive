"""Password hashing and JWT issuing.

Access tokens are short-lived and stateless. Refresh tokens are long-lived, so
they are also recorded in the ``sessions`` table -- only ever as a hash -- which
is what makes server-side logout possible. A stateless refresh token cannot be
revoked; a child's account on a shared family laptop needs it to be.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from config import settings

logger = logging.getLogger(__name__)

# bcrypt caps input at 72 bytes and silently truncates beyond it, so long
# passwords are rejected explicitly rather than quietly weakened.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


class AuthError(RuntimeError):
    """Raised when a credential or token cannot be accepted."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def validate_password_strength(password: str) -> None:
    """Raise :class:`AuthError` if a password is unusable.

    Deliberately minimal: length only. Composition rules ("one symbol, one
    digit") push people towards predictable substitutions without measurably
    helping, and this is a product parents sign up for in thirty seconds.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AuthError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt truncates beyond that)."
        )


def hash_password(password: str) -> str:
    """Hash a password with bcrypt after checking it is usable."""
    validate_password_strength(password)
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against its hash, returning False on any malformed hash."""
    try:
        return _pwd_context.verify(password, hashed)
    except Exception:  # malformed or unknown-scheme hash
        return False


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def _create_token(subject: str, token_type: TokenType, expires: timedelta) -> str:
    """Mint a signed JWT."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
        # A unique id per token, so a refresh token can be matched to its
        # session row and revoked individually.
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: str, expires_minutes: int | None = None) -> str:
    """Mint a short-lived access token for ``user_id``."""
    minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    return _create_token(user_id, "access", timedelta(minutes=minutes))


def create_refresh_token(user_id: str, expires_days: int | None = None) -> str:
    """Mint a long-lived refresh token for ``user_id``."""
    days = expires_days or settings.REFRESH_TOKEN_EXPIRE_DAYS
    return _create_token(user_id, "refresh", timedelta(days=days))


def decode_token(token: str, expect: TokenType | None = None) -> dict[str, Any]:
    """Decode and validate a JWT.

    Args:
        token: The encoded JWT.
        expect: Require this ``type`` claim. Always pass it -- without the
            check, a refresh token would be accepted as an access token, which
            hands an attacker a 30-day credential instead of a 60-minute one.

    Returns:
        The decoded claims.

    Raises:
        AuthError: If the signature, expiry or type is wrong.
    """
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise AuthError(f"Invalid or expired token: {exc}") from exc

    if expect is not None and claims.get("type") != expect:
        raise AuthError(f"Expected a {expect} token, got {claims.get('type')!r}.")
    if not claims.get("sub"):
        raise AuthError("Token is missing a subject.")
    return claims


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token for storage.

    SHA-256 rather than bcrypt: these are high-entropy random JWTs, not
    guessable secrets, and every refresh request has to look one up.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_verification_code(length: int = 32) -> str:
    """Return a URL-safe random token, for email verification or reset links."""
    return secrets.token_urlsafe(length)


__all__ = [
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "AuthError",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "generate_verification_code",
    "hash_password",
    "hash_refresh_token",
    "validate_password_strength",
    "verify_password",
]
