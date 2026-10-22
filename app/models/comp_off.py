"""Comp-off credits.

A ledger, not a counter. Each row is one day somebody worked that the calendar
later declared off — turning a balance into a number would lose which day earned
it and when that credit dies.

Expiry is *derived*, never a scheduled sweep: a row keeps ``expires_on`` and the
reader compares it against today. Nothing goes stale because a job did not run.
``expires_on`` is ``None`` when the organisation has set the expiry to zero,
meaning the credit never lapses.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class CompOffStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    USED = "USED"
    EXPIRED = "EXPIRED"


class CompOff(MongoModel):
    user_id: PyObjectId
    user_email: str | None = None

    # The day that was worked and then taken away, ``YYYY-MM-DD``.
    earned_on: str
    reason: str = Field(max_length=300)

    expires_on: str | None = None

    # Only ever AVAILABLE or USED on disk; EXPIRED is computed on read.
    status: CompOffStatus = CompOffStatus.AVAILABLE

    used_on: str | None = None
    used_at: datetime | None = None
    used_reference: str | None = None
