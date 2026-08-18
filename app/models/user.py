"""User document.

``MANAGER`` exists in the role enum but is not granted anywhere in v1. Approval
records store ``approver_role`` and users carry ``reporting_manager_id``, so
turning the role on later is a permission change, not a migration.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import EmailStr, Field

from app.models.base import MongoModel, PyObjectId


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"  # reserved — not assignable in v1
    EMPLOYEE = "EMPLOYEE"


class UserStatus(StrEnum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class User(MongoModel):
    email: EmailStr
    hashed_password: str | None = None

    first_name: str = ""
    last_name: str = ""
    phone: str | None = None

    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None

    role: UserRole = UserRole.EMPLOYEE
    status: UserStatus = UserStatus.INVITED

    shift_id: PyObjectId | None = None
    reporting_manager_id: PyObjectId | None = None

    joining_date: date | None = None
    profile_completed: bool = False
    last_login_at: datetime | None = None

    invited_by: PyObjectId | None = None
    invited_at: datetime | None = None
    activated_at: datetime | None = None

    notes: str | None = Field(default=None, max_length=2000)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email
