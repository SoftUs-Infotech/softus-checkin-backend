"""Leave categories.

Entirely admin-defined: introducing a new kind of leave is a row here, never a
code change. Existing requests keep pointing at the type they were made under.
"""

from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel


class AccrualPeriod(StrEnum):
    YEARLY = "YEARLY"  # whole quota granted at the start of the year
    MONTHLY = "MONTHLY"  # quota/12 credited each month


class LeaveType(MongoModel):
    name: str
    code: str

    annual_quota: float = Field(default=0, ge=0, le=365)
    accrual: AccrualPeriod = AccrualPeriod.YEARLY

    is_paid: bool = True
    requires_approval: bool = True
    allow_half_day: bool = True

    carry_forward_max: float = Field(default=0, ge=0, le=365)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)

    # Days after joining before an employee may use this type.
    applicable_after_days: int = Field(default=0, ge=0, le=730)

    # Empty means every department.
    applicable_departments: list[str] = Field(default_factory=list)

    color: str = "#2563eb"
    sort_order: int = 0
    is_active: bool = True
    description: str | None = Field(default=None, max_length=500)
