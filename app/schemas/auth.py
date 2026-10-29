"""Authentication request and response shapes.

The password policy lives here, in one validator, so the React form's zod schema
has a single thing to mirror.
"""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import EmploymentType, UserRole
from app.schemas.user import UserOut

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def validate_password_strength(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long.")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must contain at least one letter.")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one number.")
    return value


class _PasswordMixin(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)


class InviteCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(default="", max_length=60)
    last_name: str = Field(default="", max_length=60)
    role: UserRole = UserRole.EMPLOYEE
    designation: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    employee_code: str | None = Field(default=None, max_length=40)
    shift_id: str | None = None
    reporting_manager_id: str | None = None
    joining_date: date | None = None


class InviteOut(BaseModel):
    id: str
    email: EmailStr
    role: UserRole
    status: str
    expires_at: str
    invite_url: str | None = None


class InvitePreview(BaseModel):
    """What the public accept-invite page shows before a password is chosen."""

    email: EmailStr
    first_name: str = ""
    last_name: str = ""
    role: UserRole
    expires_at: str


class InviteAccept(_PasswordMixin):
    token: str
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(default="", max_length=60)
    phone: str | None = Field(default=None, max_length=20)
    designation: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None
    all_devices: bool = False


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tokens: TokenPair
    user: UserOut


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)


class TwoFactorChallengeOut(BaseModel):
    """What ``/auth/login`` returns instead of tokens when 2FA is switched on.

    ``two_factor_required`` is a constant rather than something the client has to
    infer from a missing ``tokens`` key.
    """

    two_factor_required: Literal[True] = True
    challenge_token: str
    expires_in: int


class TwoFactorVerifyRequest(BaseModel):
    challenge_token: str
    # Wide enough for a recovery code, which is offered in place of the digits.
    code: str = Field(min_length=1, max_length=64)


class TwoFactorCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TwoFactorDisableRequest(BaseModel):
    # The password, not just a session: a borrowed unlocked laptop must not be
    # enough to take the second factor back off.
    password: str


class TwoFactorSetupOut(BaseModel):
    """Returned exactly once, by setup. Nothing here is retrievable afterwards."""

    secret: str
    otpauth_uri: str
    recovery_codes: list[str]


class TwoFactorStatus(BaseModel):
    enabled: bool
    #: A secret has been generated but no code has confirmed it yet.
    pending: bool = False
    confirmed_at: datetime | None = None
    recovery_codes_remaining: int = 0


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)
