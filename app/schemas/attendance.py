"""Attendance API shapes."""

from datetime import date as date_type
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.attendance import STATUS_COLORS, STATUS_LABELS, AttendanceStatus


class GeoLocation(BaseModel):
    """Required on every punch — the organisation records where work started."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_metres: float | None = Field(default=None, ge=0, le=100_000)
    address: str | None = Field(default=None, max_length=300)


class PunchRequestBody(BaseModel):
    location: GeoLocation | None = None
    note: str | None = Field(default=None, max_length=300)
    # Any stable string per user action; a repeat is treated as the same punch.
    request_key: str | None = Field(default=None, min_length=8, max_length=100)


class PunchEventOut(BaseModel):
    """One punch, including where and what it came from.

    The device, platform and address are what an administrator reads on the
    register when a punch looks wrong; ``outside_office`` is a flag on the day,
    never a rejection of it.
    """

    model_config = ConfigDict(from_attributes=True)

    at: datetime
    latitude: float | None = None
    longitude: float | None = None
    accuracy_metres: float | None = None
    address: str | None = None
    city: str | None = None
    distance_metres: int | None = None
    outside_office: bool = False

    ip: str | None = None
    user_agent: str | None = None
    device_class: str | None = None
    platform: str | None = None
    source: str = "WEB"
    note: str | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    punch_in: PunchEventOut
    punch_out: PunchEventOut | None = None
    auto_closed: bool = False
    duration_minutes: int = 0


class AttendanceDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: str
    status: AttendanceStatus
    status_label: str = ""
    color: str = "grey"

    worked_minutes: int = 0
    worked_display: str = "0m"
    expected_minutes: int = 0
    shortfall_minutes: int = 0

    is_late: bool = False
    is_open: bool = False
    is_manually_adjusted: bool = False

    first_punch_in: datetime | None = None
    last_punch_out: datetime | None = None
    sessions: list[SessionOut] = Field(default_factory=list)

    holiday_name: str | None = None
    leave_type: str | None = None
    leave_day_part: str | None = None
    adjustment_note: str | None = None

    user_id: str | None = None
    employee_name: str | None = None
    # Carried on register rows so a search result can show what it matched on.
    employee_email: str | None = None
    employee_code: str | None = None
    department: str | None = None
    #: No shift assignment covered this date. The punches still count.
    no_shift_assigned: bool = False


class MonthSummary(BaseModel):
    present_days: int = 0
    half_days: int = 0
    absent_days: int = 0
    missing_punch_out_days: int = 0
    leave_days: int = 0
    holidays: int = 0
    weekly_offs: int = 0
    late_days: int = 0
    worked_minutes: int = 0
    worked_display: str = "0m"
    expected_minutes: int = 0
    working_days: int = 0


class MonthCalendarOut(BaseModel):
    year: int
    month: int
    from_date: str
    to_date: str
    days: list[AttendanceDayOut]
    summary: MonthSummary
    legend: dict[str, str] = Field(default_factory=lambda: dict(STATUS_COLORS))
    labels: dict[str, str] = Field(default_factory=lambda: dict(STATUS_LABELS))


class TodayOut(BaseModel):
    """Everything the dashboard punch widget needs in one call."""

    server_time: datetime
    date: str
    day: AttendanceDayOut
    can_punch_in: bool
    can_punch_out: bool
    blocked_reason: str | None = None
    #: True when no shift assignment covers today. Never blocks a punch.
    no_shift_assigned: bool = False
    shift: dict[str, Any] | None = None
    device_class: str = "UNKNOWN"
    geolocation_required: bool = True
    elapsed_minutes: int = 0


class AdminSessionInput(BaseModel):
    punch_in: datetime
    punch_out: datetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "AdminSessionInput":
        if self.punch_out and self.punch_out <= self.punch_in:
            raise ValueError("Punch-out must be after punch-in.")
        return self


class AttendanceAdjust(BaseModel):
    """An administrator rewriting a day. Every field is replaced, not merged."""

    sessions: list[AdminSessionInput] = Field(default_factory=list, max_length=20)
    note: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def _sessions_do_not_overlap(self) -> "AttendanceAdjust":
        ordered = sorted(self.sessions, key=lambda item: item.punch_in)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.punch_out is None or earlier.punch_out > later.punch_in:
                raise ValueError("Sessions must not overlap.")
        return self


class AttendanceQuery(BaseModel):
    from_date: date_type | None = None
    to_date: date_type | None = None
    user_id: str | None = None
    status: AttendanceStatus | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "AttendanceQuery":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("'from_date' must not be after 'to_date'.")
        return self


class RegisterSort(StrEnum):
    EMPLOYEE = "employee"
    WORKED_MINUTES = "worked_minutes"
    FIRST_PUNCH_IN = "first_punch_in"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class RegisterQuery(BaseModel):
    """What the daily register was asked for.

    ``day`` and ``from_date``/``to_date`` are alternatives rather than layers —
    the service refuses both at once instead of silently preferring one, because
    a date control that appears to be ignored is worse than an error.
    """

    day: date_type | None = None
    from_date: date_type | None = None
    to_date: date_type | None = None

    search: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    status: AttendanceStatus | None = None
    exceptions_only: bool = False

    sort: RegisterSort | None = None
    direction: SortDirection = SortDirection.ASC

    @property
    def is_filtered(self) -> bool:
        return bool(self.search or self.department or self.status or self.exceptions_only)


class RegisterMeta(BaseModel):
    """What the register actually answered, so the screen can say so.

    ``employees`` is the headcount the filters left, which is how the client
    tells "nobody matched the search" apart from "nothing happened that day".
    """

    from_date: str
    to_date: str
    days: int
    employees: int
    total: int
    returned: int
    truncated: bool
    filtered: bool
