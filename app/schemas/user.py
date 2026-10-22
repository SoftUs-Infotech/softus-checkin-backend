"""What the API accepts and returns for users.

Deliberately separate from ``app/models/user.py`` — ``hashed_password`` exists on
the model and can never appear here.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole, UserStatus, may_punch


class UserProfileUpdate(BaseModel):
    """What an employee may change about themselves."""

    first_name: str | None = Field(default=None, min_length=1, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=20)
    designation: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)


class UserAdminUpdate(UserProfileUpdate):
    """Everything an administrator may additionally change."""

    employee_code: str | None = Field(default=None, max_length=40)
    role: UserRole | None = None

    # A shift is assigned for a stretch of work, so it carries a window. Both
    # dates absent means the assignment simply runs until it is changed.
    shift_id: str | None = None
    shift_from: date | None = None
    shift_to: date | None = None
    reporting_manager_id: str | None = None
    joining_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    # An administrator is an overseer by default; a working admin gets the
    # personal side of the app back with this.
    can_punch: bool | None = None

    # Work from home lifts the office-network restriction for this person only.
    wfh_enabled: bool | None = None
    wfh_from: date | None = None
    wfh_to: date | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    phone: str | None = None
    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None
    role: UserRole
    status: UserStatus
    shift_id: str | None = None
    shift_from: date | None = None
    shift_to: date | None = None
    reporting_manager_id: str | None = None
    joining_date: date | None = None
    profile_completed: bool = False
    can_punch: bool = True
    wfh_enabled: bool = False
    wfh_from: date | None = None
    wfh_to: date | None = None
    last_login_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "UserOut":
        data = dict(document)
        first = data.get("first_name") or ""
        last = data.get("last_name") or ""
        data["full_name"] = f"{first} {last}".strip() or data.get("email", "")
        # Not `data.get("can_punch", True)`: an account created before the flag
        # existed has no value, and an administrator must not inherit "yes".
        data["can_punch"] = may_punch(data)
        return cls.model_validate(data)


class UserListFilters(BaseModel):
    search: str | None = None
    role: UserRole | None = None
    status: UserStatus | None = None
    department: str | None = None
