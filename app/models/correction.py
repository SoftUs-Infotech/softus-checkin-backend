"""Attendance correction (regularisation) requests.

When an employee forgets to punch, they ask for the day to be fixed rather than
editing it themselves. Approval rewrites the day's sessions and recomputes it —
the same path an admin adjustment takes, so the two can never disagree.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class CorrectionType(StrEnum):
    MISSED_PUNCH_IN = "MISSED_PUNCH_IN"
    MISSED_PUNCH_OUT = "MISSED_PUNCH_OUT"
    WRONG_TIME = "WRONG_TIME"
    FULL_DAY = "FULL_DAY"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class Correction(MongoModel):
    user_id: PyObjectId
    user_email: str | None = None
    user_name: str | None = None

    date: str  # YYYY-MM-DD in the organisation's timezone
    type: CorrectionType

    requested_punch_in: datetime | None = None
    requested_punch_out: datetime | None = None
    reason: str = Field(max_length=1000)

    status: ApprovalStatus = ApprovalStatus.PENDING

    # ``approver_role`` is stored alongside the id so that enabling the MANAGER
    # role later does not make historical approvals ambiguous.
    approver_id: PyObjectId | None = None
    approver_email: str | None = None
    approver_role: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = Field(default=None, max_length=500)

    # What the day looked like before approval, so a reversal is possible.
    previous_status: str | None = None
    previous_worked_minutes: int | None = None
