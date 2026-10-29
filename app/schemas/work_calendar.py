"""Work-calendar API shapes."""

from datetime import date as date_type

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.work_calendar import DayKind, OverrideSource

# ``date`` is aliased because these models have a field of that name: in
# ``x: ann = value`` CPython binds ``x`` before evaluating ``ann``.


class OverrideCreate(BaseModel):
    date: date_type
    kind: DayKind
    reason: str = Field(min_length=3, max_length=300)


class FullDayCreate(BaseModel):
    """Count a date as a full day for everybody who punched in."""

    date: date_type
    reason: str = Field(default="", max_length=300)


class FullDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    date: str
    reason: str = ""
    set_by_email: str | None = None


class SwapCreate(BaseModel):
    """"The third Saturday off instead of the second" — both halves, one call."""

    off_date: date_type
    working_date: date_type
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def _two_distinct_dates(self) -> "SwapCreate":
        if self.off_date == self.working_date:
            raise ValueError("'off_date' and 'working_date' must differ.")
        return self


class OverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    date: str
    kind: DayKind
    reason: str
    source: OverrideSource = OverrideSource.MANUAL
    swap_with_date: str | None = None
    set_by_email: str | None = None


class DayOut(BaseModel):
    """One cell of the calendar, with the rule that decided it."""

    date: str
    weekday: str
    is_working: bool
    source: str
    reason: str
    is_undecided: bool = False
    swap_with_date: str | None = None
    holiday_name: str | None = None
    #: Counted as a full day for everybody who punched in, however long they stayed.
    full_day: bool = False
    full_day_reason: str | None = None

    @classmethod
    def from_decision(cls, decision, full_day: dict | None = None) -> "DayOut":
        from app.schemas.shift import WEEKDAY_NAMES

        return cls(
            date=decision.date,
            weekday=WEEKDAY_NAMES[decision.day.weekday()],
            is_working=decision.is_working,
            source=decision.source,
            reason=decision.reason,
            is_undecided=decision.is_undecided,
            swap_with_date=decision.swap_with_date,
            holiday_name=decision.holiday_name,
            full_day=full_day is not None,
            full_day_reason=(full_day or {}).get("reason") or None,
        )


class MonthCalendarOut(BaseModel):
    year: int
    month: int
    working_days: int
    days: list[DayOut]
