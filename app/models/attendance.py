"""Attendance documents.

One document per employee per organisation-local day, holding every punch
session for that day. Keyed by ``(user_id, date)`` with a unique index, so a
double submission cannot create two records for the same day.

The shift is snapshotted onto the day. Changing a shift's required hours next
month must not silently rewrite what last month's attendance meant.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class AttendanceStatus(StrEnum):
    PRESENT = "PRESENT"
    HALF_DAY = "HALF_DAY"
    SHORT_HOURS = "SHORT_HOURS"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_STARTED = "NOT_STARTED"
    MISSING_PUNCH_OUT = "MISSING_PUNCH_OUT"
    ABSENT = "ABSENT"
    ON_LEAVE = "ON_LEAVE"
    HOLIDAY = "HOLIDAY"
    WEEKEND = "WEEKEND"
    UPCOMING = "UPCOMING"
    #: A day before the employee's first — nothing was expected of them, and
    #: nothing is owed for it except the pay they had not yet started earning.
    BEFORE_JOINING = "BEFORE_JOINING"
    #: A day before the organisation started tracking attendance — before the
    #: administrator's "reset" on go-live. People worked; the app was not there.
    NOT_TRACKED = "NOT_TRACKED"


#: What each status looks like on the calendar. The frontend reads this rather
#: than duplicating the mapping.
STATUS_COLORS: dict[str, str] = {
    AttendanceStatus.PRESENT: "green",
    AttendanceStatus.HALF_DAY: "amber",
    AttendanceStatus.SHORT_HOURS: "amber",
    AttendanceStatus.IN_PROGRESS: "blue",
    AttendanceStatus.NOT_STARTED: "grey",
    AttendanceStatus.MISSING_PUNCH_OUT: "red",
    AttendanceStatus.ABSENT: "red",
    AttendanceStatus.ON_LEAVE: "violet",
    AttendanceStatus.HOLIDAY: "yellow",
    AttendanceStatus.WEEKEND: "grey",
    AttendanceStatus.UPCOMING: "grey",
    AttendanceStatus.BEFORE_JOINING: "grey",
    AttendanceStatus.NOT_TRACKED: "grey",
}

STATUS_LABELS: dict[str, str] = {
    AttendanceStatus.PRESENT: "Present",
    AttendanceStatus.HALF_DAY: "Half day",
    AttendanceStatus.SHORT_HOURS: "Short hours",
    AttendanceStatus.IN_PROGRESS: "Working",
    AttendanceStatus.NOT_STARTED: "Not started",
    AttendanceStatus.MISSING_PUNCH_OUT: "Missing punch-out",
    AttendanceStatus.ABSENT: "Absent",
    AttendanceStatus.ON_LEAVE: "On leave",
    AttendanceStatus.HOLIDAY: "Holiday",
    AttendanceStatus.WEEKEND: "Weekly off",
    AttendanceStatus.UPCOMING: "Upcoming",
    AttendanceStatus.BEFORE_JOINING: "Before joining",
    AttendanceStatus.NOT_TRACKED: "Not tracked",
}

#: Statuses that mean the employee is not expected to punch.
NON_WORKING_STATUSES = {
    AttendanceStatus.HOLIDAY,
    AttendanceStatus.WEEKEND,
    AttendanceStatus.ON_LEAVE,
    AttendanceStatus.UPCOMING,
    AttendanceStatus.BEFORE_JOINING,
    AttendanceStatus.NOT_TRACKED,
}


class PunchSource(StrEnum):
    WEB = "WEB"
    ADMIN = "ADMIN"
    CORRECTION = "CORRECTION"
    SYSTEM = "SYSTEM"


class PunchEvent(MongoModel):
    at: datetime
    latitude: float | None = None
    longitude: float | None = None
    accuracy_metres: float | None = None
    address: str | None = None
    # The town or city the coordinates fall in. Held separately from the full
    # address because it is the only part that fits a register column, and the
    # only part an administrator scanning a list actually reads.
    city: str | None = None

    ip: str | None = None
    user_agent: str | None = None
    device_class: str | None = None
    platform: str | None = None

    # How far the punch was from the office, when the office has coordinates.
    # A flag, never a refusal: a bad GPS fix must not cost an attendance day.

    source: PunchSource = PunchSource.WEB
    note: str | None = Field(default=None, max_length=300)


class AttendanceSession(MongoModel):
    punch_in: PunchEvent
    punch_out: PunchEvent | None = None
    auto_closed: bool = False

    @property
    def is_open(self) -> bool:
        return self.punch_out is None


class Attendance(MongoModel):
    user_id: PyObjectId
    date: str  # YYYY-MM-DD in the organisation's timezone

    shift_id: PyObjectId | None = None
    shift_snapshot: dict[str, Any] | None = None

    sessions: list[AttendanceSession] = Field(default_factory=list)

    worked_minutes: int = 0
    status: AttendanceStatus = AttendanceStatus.NOT_STARTED
    is_late: bool = False

    first_punch_in: datetime | None = None
    last_punch_out: datetime | None = None

    is_manually_adjusted: bool = False
    adjusted_by: PyObjectId | None = None
    adjusted_at: datetime | None = None
    adjustment_note: str | None = Field(default=None, max_length=500)


class PunchRequest(MongoModel):
    """Idempotency guard.

    The client sends a key with each punch; a unique index on
    ``(user_id, request_key)`` turns a double-click or a retried request into a
    no-op instead of a second punch. Rows expire after a day.
    """

    user_id: PyObjectId
    request_key: str
    action: str
    expires_at: datetime
