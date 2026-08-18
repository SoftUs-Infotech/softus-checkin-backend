"""Invitation and password-reset documents.

Only the SHA-256 digest of a token is stored. A database leak therefore yields
no usable invite or reset links.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import EmailStr

from app.models.base import MongoModel, PyObjectId
from app.models.user import UserRole


class InviteStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REVOKED = "REVOKED"


class Invite(MongoModel):
    email: EmailStr
    user_id: PyObjectId
    token_hash: str
    role: UserRole = UserRole.EMPLOYEE
    status: InviteStatus = InviteStatus.PENDING
    expires_at: datetime
    invited_by: PyObjectId | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class PasswordReset(MongoModel):
    user_id: PyObjectId
    token_hash: str
    expires_at: datetime
    used_at: datetime | None = None


class RefreshToken(MongoModel):
    """One row per issued refresh token.

    Rotation replaces a row rather than mutating it, so presenting an already
    rotated token is detectable and revokes the entire family.
    """

    user_id: PyObjectId
    token_hash: str
    family_id: str
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: str | None = None
    user_agent: str | None = None
    ip: str | None = None
