"""Password hashing and JWT issue/verify.

Passwords are pre-hashed with SHA-256 before bcrypt so that long passphrases are
not silently truncated at bcrypt's 72-byte input limit.

Opaque tokens (invites, password resets, refresh tokens) never touch the database
in plaintext — only their SHA-256 digest is stored, so a database leak does not
hand out working links.
"""

import base64
import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any, Literal

import bcrypt
import jwt
from jwt import PyJWTError

from app.core.config import settings
from app.core.datetime import utc_now
from app.core.exceptions import AuthenticationError

TokenType = Literal["access", "refresh"]


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
def _prepare_password(password: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare_password(password), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database — treat as a failed login, not a crash.
        return False


# --------------------------------------------------------------------------- #
# opaque tokens
# --------------------------------------------------------------------------- #
def generate_token(length: int = 48) -> str:
    """A URL-safe random token to embed in an email link."""
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """Digest stored in place of the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_token(token), stored_hash)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    issued_at = utc_now()
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(
    subject: str,
    *,
    role: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    claims = dict(extra_claims or {})
    if role is not None:
        claims["role"] = role
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        claims,
    )


def create_refresh_token(
    subject: str,
    *,
    family_id: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(token, family_id)``.

    The family id links a rotation chain: if an already-rotated token is ever
    presented again, the whole family is revoked as a suspected replay.
    """
    family = family_id or uuid.uuid4().hex
    claims = dict(extra_claims or {})
    claims["fam"] = family
    token = _create_token(
        subject,
        "refresh",
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        claims,
    )
    return token, family


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except PyJWTError as exc:
        raise AuthenticationError(
            "Your session is invalid or has expired.", code="INVALID_TOKEN"
        ) from exc

    if expected_type and payload.get("type") != expected_type:
        raise AuthenticationError(
            "Your session is invalid or has expired.", code="INVALID_TOKEN_TYPE"
        )
    if not payload.get("sub"):
        raise AuthenticationError(
            "Your session is invalid or has expired.", code="INVALID_TOKEN"
        )
    return payload
