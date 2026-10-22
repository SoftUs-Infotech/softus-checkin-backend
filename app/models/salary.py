"""Compensation records. Administrators only — never serialised to an employee.

Salary is stored as **history**: a raise inserts a row, it never edits the one
before it. Last month's payout must keep resolving to the figure that was in
force when it was earned, and a single mutable field on the user document cannot
do that.

An adjustment carries ``days``, never money. A late-approved August leave is
"give back one day" — what that day is worth depends on August's working-day
count and the CTC in force then, both of which the projection already knows.
Storing an amount would freeze a number that the rules can still change.
"""

from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class SalaryAdjustmentKind(StrEnum):
    LATE_LEAVE_APPROVAL = "LATE_LEAVE_APPROVAL"
    LATE_CORRECTION = "LATE_CORRECTION"
    COMP_OFF = "COMP_OFF"
    MANUAL = "MANUAL"


class Salary(MongoModel):
    user_id: PyObjectId
    monthly_ctc: float = Field(ge=0)

    # ``YYYY-MM-DD``; the first day this figure applies from.
    effective_from: str
    note: str | None = Field(default=None, max_length=300)

    set_by: PyObjectId | None = None
    set_by_email: str | None = None


class SalaryAdjustment(MongoModel):
    user_id: PyObjectId

    # ``YYYY-MM`` — the month whose payout carries the correction.
    apply_month: str
    # Where it came from, so the payslip line can explain itself.
    source_month: str | None = None
    source_date: str | None = None

    kind: SalaryAdjustmentKind = SalaryAdjustmentKind.MANUAL

    # Positive pays a day back, negative takes one away.
    days: float

    reason: str = Field(max_length=300)

    reference_type: str | None = None
    reference_id: str | None = None

    created_by: PyObjectId | None = None
