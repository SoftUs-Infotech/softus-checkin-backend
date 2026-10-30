"""User document.

``MANAGER`` exists in the role enum but is not granted anywhere in v1. Approval
records store ``approver_role`` and users carry ``reporting_manager_id``, so
turning the role on later is a permission change, not a migration.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.models.base import MongoModel, PyObjectId


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"  # reserved — not assignable in v1
    EMPLOYEE = "EMPLOYEE"


class EmploymentType(StrEnum):
    """How somebody is engaged — which decides how their pay is worked out."""

    FULL_TIME = "FULL_TIME"
    # Interns and part-timers: paid a fixed monthly amount in proportion to the
    # hours they put in, rather than docked by the day.
    PART_TIME = "PART_TIME"


class UserStatus(StrEnum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class TwoFactorChallenge(BaseModel):
    """A sign-in that cleared the password and is waiting on a code."""

    token_hash: str
    expires_at: datetime
    # The account's password hash, digested. Changing the password rewrites that
    # hash, so an outstanding challenge stops matching on its own — nothing has
    # to remember to go and delete it.
    password_fingerprint: str


class TwoFactor(BaseModel):
    """What two-factor authentication stores on an account.

    ``secret`` exists from the moment setup is called, but ``enabled`` only turns
    true once a code proves the secret reached the authenticator app intact. A
    mistyped QR scan therefore cannot lock an administrator out of their own
    organisation.
    """

    enabled: bool = False
    secret: str | None = None
    confirmed_at: datetime | None = None
    # Digests, never the codes themselves. A recovery code is a random token, so
    # a token digest is the right primitive here — the same reasoning that keeps
    # invite and reset links out of the database in plaintext.
    recovery_code_hashes: list[str] = Field(default_factory=list)
    # The 30-second step a code was last accepted for. Anything at or below it is
    # already spent, which is what stops a code read over somebody's shoulder
    # being replayed inside its own validity window.
    last_used_step: int | None = None
    challenge: TwoFactorChallenge | None = None


def two_factor_of(document: dict[str, Any]) -> dict[str, Any]:
    """The 2FA sub-document, or an empty one for accounts that predate it."""
    stored = document.get("two_factor")
    return stored if isinstance(stored, dict) else {}


def two_factor_required(document: dict[str, Any]) -> bool:
    """Whether this sign-in has to be finished with a code.

    Read from the stored flag rather than from the role: an administrator who is
    later demoted keeps the protection they switched on.
    """
    state = two_factor_of(document)
    return bool(state.get("enabled") and state.get("secret"))


class User(MongoModel):
    email: EmailStr
    hashed_password: str | None = None

    first_name: str = ""
    last_name: str = ""
    phone: str | None = None

    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME

    role: UserRole = UserRole.EMPLOYEE
    status: UserStatus = UserStatus.INVITED

    # A shift is a *planned window* for a piece of work, not a permanent
    # property of the person: "Riya is on the general shift for these two
    # weeks". Outside the window there is no active shift, which is a signal
    # rather than a fault — see ``active_shift_window``.
    #
    # Both dates open-ended means the assignment simply runs until changed,
    # which is how every account created before this existed behaves.
    shift_id: PyObjectId | None = None
    shift_from: date | None = None
    shift_to: date | None = None

    reporting_manager_id: PyObjectId | None = None

    joining_date: date | None = None
    profile_completed: bool = False
    last_login_at: datetime | None = None

    invited_by: PyObjectId | None = None
    invited_at: datetime | None = None
    activated_at: datetime | None = None

    # Administrators do not punch by default; a working admin can be given the
    # personal side of the app back with this flag. See ``default_can_punch``.
    can_punch: bool = True


    notes: str | None = Field(default=None, max_length=2000)

    two_factor: TwoFactor = Field(default_factory=lambda: TwoFactor())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email


def default_can_punch(role: UserRole) -> bool:
    """An administrator oversees attendance rather than recording it.

    A working administrator — an HR manager who also punches — is a real case,
    so this is only the default an account starts with, not a rule.
    """
    return role is not UserRole.ADMIN


def may_punch(document: dict[str, Any]) -> bool:
    """Whether this account has the personal side of the app.

    Read from the document when it says, and from the role when it does not.
    Accounts created before the flag existed have no value stored, and reading
    a missing value as "yes" would hand every existing administrator a punch
    widget they are not supposed to have.
    """
    stored = document.get("can_punch")
    if isinstance(stored, bool):
        return stored
    role = document.get("role", UserRole.EMPLOYEE)
    return default_can_punch(UserRole(role) if role else UserRole.EMPLOYEE)
