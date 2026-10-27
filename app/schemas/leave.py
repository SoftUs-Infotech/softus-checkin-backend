"""Leave request and balance API shapes."""

from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.correction import ApprovalStatus
from app.models.leave import DayPart


class LeaveApply(BaseModel):
    leave_type_id: str
    from_date: date_type
    to_date: date_type
    day_part: DayPart = DayPart.FULL
    reason: str = Field(min_length=5, max_length=1000)
    contact_number: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _check_range(self) -> "LeaveApply":
        if self.to_date < self.from_date:
            raise ValueError("'to_date' must not be before 'from_date'.")
        if self.day_part is not DayPart.FULL and self.from_date != self.to_date:
            raise ValueError("A half day can only be applied for a single date.")
        if (self.to_date - self.from_date).days > 365:
            raise ValueError("A single request cannot span more than a year.")
        return self


class LeaveDecision(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class LeaveDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: str
    day_part: DayPart


class LeaveRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    user_email: str | None = None
    user_name: str | None = None
    leave_type_id: str
    leave_type_name: str | None = None
    leave_type_code: str | None = None
    from_date: str
    to_date: str
    days: list[LeaveDayOut] = Field(default_factory=list)
    total_days: float
    reason: str
    contact_number: str | None = None
    status: ApprovalStatus
    approver_email: str | None = None
    approver_role: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict) -> "LeaveRequestOut":
        return cls.model_validate(document)


class LeaveBalanceOut(BaseModel):
    leave_type_id: str
    leave_type_name: str
    leave_type_code: str
    color: str = "#2563eb"
    is_paid: bool = True
    allow_half_day: bool = True
    allow_negative_balance: bool = False
    visible_to_employees: bool = True
    year: int
    allocated: float
    carried_forward: float
    used: float
    pending: float
    available: float


class LeaveBalanceAdjust(BaseModel):
    """An administrator correcting an allocation, with a reason on the record."""

    allocated: float | None = Field(default=None, ge=0, le=365)
    carried_forward: float | None = Field(default=None, ge=0, le=365)
    used: float | None = Field(default=None, ge=0, le=365)
    note: str = Field(min_length=3, max_length=500)
