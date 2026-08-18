"""What the API accepts and returns for users.

Deliberately separate from ``app/models/user.py`` — ``hashed_password`` exists on
the model and can never appear here.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import UserRole, UserStatus


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
    shift_id: str | None = None
    reporting_manager_id: str | None = None
    joining_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("role")
    @classmethod
    def _manager_role_is_reserved(cls, value: UserRole | None) -> UserRole | None:
        if value == UserRole.MANAGER:
            raise ValueError("The MANAGER role is reserved and cannot be assigned yet.")
        return value


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
    reporting_manager_id: str | None = None
    joining_date: date | None = None
    profile_completed: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "UserOut":
        data = dict(document)
        first = data.get("first_name") or ""
        last = data.get("last_name") or ""
        data["full_name"] = f"{first} {last}".strip() or data.get("email", "")
        return cls.model_validate(data)


class UserListFilters(BaseModel):
    search: str | None = None
    role: UserRole | None = None
    status: UserStatus | None = None
    department: str | None = None
