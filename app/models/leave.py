"""Leave requests and balances.

A request stores the exact days it covers, each with a day part, rather than
just a range. Weekends and holidays are excluded when the request is created, so
the deduction matches what an employee would count by hand — and the stored days
are what the attendance calendar reads.

Balances are per employee, per leave type, per year. ``pending`` is reserved at
the moment of application so two overlapping requests cannot both be approved
against the same remaining day. Each balance also carries the ledger of accrual
credits already applied to it, which is what stops the accrual job crediting the
same cycle twice.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.datetime import utc_now
from app.models.base import MongoModel, PyObjectId
from app.models.correction import ApprovalStatus


class DayPart(StrEnum):
    FULL = "FULL"
    FIRST_HALF = "FIRST_HALF"
    SECOND_HALF = "SECOND_HALF"


#: How much of a day each part consumes.
DAY_PART_VALUE: dict[str, float] = {
    DayPart.FULL: 1.0,
    DayPart.FIRST_HALF: 0.5,
    DayPart.SECOND_HALF: 0.5,
}


class LeaveDay(MongoModel):
    date: str  # YYYY-MM-DD in the organisation's timezone
    day_part: DayPart = DayPart.FULL


class LeaveRequest(MongoModel):
    user_id: PyObjectId
    user_email: str | None = None
    user_name: str | None = None

    leave_type_id: PyObjectId
    leave_type_name: str | None = None
    leave_type_code: str | None = None

    from_date: str
    to_date: str
    days: list[LeaveDay] = Field(default_factory=list)
    total_days: float = 0

    reason: str = Field(max_length=1000)
    contact_number: str | None = Field(default=None, max_length=20)

    status: ApprovalStatus = ApprovalStatus.PENDING

    approver_id: PyObjectId | None = None
    approver_email: str | None = None
    approver_role: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = Field(default=None, max_length=500)


class AccrualCredit(BaseModel):
    """One cycle's credit, kept so a second run of the job credits nothing.

    The cycle key ("2026-M08", "2026-Q3", "2026-Y") is unique within a balance,
    which is what makes the accrual job idempotent.
    """

    cycle_key: str
    days: float = 0
    credited_at: datetime = Field(default_factory=utc_now)


class LeaveBalance(MongoModel):
    user_id: PyObjectId
    leave_type_id: PyObjectId
    year: int

    allocated: float = 0
    carried_forward: float = 0
    used: float = 0
    pending: float = 0

    accrual_credits: list[AccrualCredit] = Field(default_factory=list)

    @property
    def available(self) -> float:
        return self.allocated + self.carried_forward - self.used - self.pending
