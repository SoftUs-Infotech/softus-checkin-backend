"""Shift API shapes.

Single-field rules live here. Cross-field rules that depend on stored values
(``half_day_minutes`` versus ``required_minutes`` on a partial update) are
checked in the service after merging, where both values are known.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
CODE_PATTERN = re.compile(r"^[A-Z0-9_]{2,20}$")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _validate_time(value: str) -> str:
    if not TIME_PATTERN.match(value):
        raise ValueError("Time must be in 24-hour HH:MM format, for example 09:30.")
    return value


def _validate_weekdays(value: list[int]) -> list[int]:
    if not value:
        raise ValueError("A shift must have at least one working weekday.")
    unique = sorted(set(value))
    if any(day < 0 or day > 6 for day in unique):
        raise ValueError("Weekdays must be between 0 (Monday) and 6 (Sunday).")
    return unique


class ShiftBase(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    start_time: str = "09:30"
    end_time: str = "18:30"
    required_minutes: int = Field(default=540, ge=1, le=24 * 60)
    half_day_minutes: int = Field(default=240, ge=1, le=24 * 60)
    grace_minutes: int = Field(default=15, ge=0, le=240)
    late_window_minutes: int = Field(default=0, ge=0, le=12 * 60)
    break_minutes: int = Field(default=0, ge=0, le=8 * 60)
    working_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    description: str | None = Field(default=None, max_length=500)

    _check_start = field_validator("start_time")(_validate_time)
    _check_end = field_validator("end_time")(_validate_time)
    _check_weekdays = field_validator("working_weekdays")(_validate_weekdays)


class ShiftCreate(ShiftBase):
    code: str = Field(min_length=2, max_length=20)
    is_default: bool = False

    @field_validator("code")
    @classmethod
    def _normalise_code(cls, value: str) -> str:
        normalised = value.strip().upper().replace(" ", "_").replace("-", "_")
        if not CODE_PATTERN.match(normalised):
            raise ValueError("Code may use only letters, numbers and underscores (2–20 chars).")
        return normalised

    @model_validator(mode="after")
    def _half_day_below_full_day(self) -> "ShiftCreate":
        if self.half_day_minutes >= self.required_minutes:
            raise ValueError(
                "Half-day hours must be less than the hours required for a full day."
            )
        return self


class ShiftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    start_time: str | None = None
    end_time: str | None = None
    required_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    half_day_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    grace_minutes: int | None = Field(default=None, ge=0, le=240)
    late_window_minutes: int | None = Field(default=None, ge=0, le=12 * 60)
    break_minutes: int | None = Field(default=None, ge=0, le=8 * 60)
    working_weekdays: list[int] | None = None
    description: str | None = Field(default=None, max_length=500)
    is_default: bool | None = None
    is_active: bool | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _check_times(cls, value: str | None) -> str | None:
        return None if value is None else _validate_time(value)

    @field_validator("working_weekdays")
    @classmethod
    def _check_weekdays(cls, value: list[int] | None) -> list[int] | None:
        return None if value is None else _validate_weekdays(value)


class ShiftAssign(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=500)


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str
    start_time: str
    end_time: str
    required_minutes: int
    half_day_minutes: int
    grace_minutes: int
    late_window_minutes: int = 0
    break_minutes: int
    working_weekdays: list[int]
    working_weekday_names: list[str] = Field(default_factory=list)
    crosses_midnight: bool = False
    is_default: bool = False
    is_active: bool = True
    description: str | None = None
    assigned_count: int | None = None

    @classmethod
    def from_document(cls, document: dict, *, assigned_count: int | None = None) -> "ShiftOut":
        data = dict(document)
        weekdays = data.get("working_weekdays") or []
        data["working_weekday_names"] = [WEEKDAY_NAMES[day] for day in weekdays if 0 <= day <= 6]
        data["crosses_midnight"] = data.get("end_time", "") <= data.get("start_time", "")
        data["assigned_count"] = assigned_count
        return cls.model_validate(data)
