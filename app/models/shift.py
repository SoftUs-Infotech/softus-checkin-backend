"""Shift definitions — the rules that decide what a full day of work means.

A shift is data, not code. Changing required hours, grace, or which weekdays
count is an admin action, and the attendance engine reads whatever is here.

``working_weekdays`` uses Python's convention: Monday is 0, Sunday is 6.
"""

from enum import IntEnum

from pydantic import Field

from app.models.base import MongoModel


class Weekday(IntEnum):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


class Shift(MongoModel):
    name: str
    code: str

    # Local wall-clock times in the organisation's timezone, ``HH:MM``.
    start_time: str = "09:30"
    end_time: str = "18:30"

    required_minutes: int = Field(default=540, ge=1, le=24 * 60)
    half_day_minutes: int = Field(default=240, ge=1, le=24 * 60)
    grace_minutes: int = Field(default=15, ge=0, le=240)
    # How long after the grace period an arrival still counts as late. Past it,
    # somebody is arriving for the afternoon — an intern coming from college —
    # not arriving late for the morning. 0 means no limit: anything after the
    # grace period is late, which is how every shift behaved before this.
    late_window_minutes: int = Field(default=0, ge=0, le=12 * 60)
    break_minutes: int = Field(default=0, ge=0, le=8 * 60)

    working_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])

    is_default: bool = False
    is_active: bool = True
    description: str | None = Field(default=None, max_length=500)

    @property
    def crosses_midnight(self) -> bool:
        """A night shift whose end time is earlier in the day than its start."""
        return self.end_time <= self.start_time
