"""Organisation settings — a single document holding attendance policy.

The timezone deliberately lives in the environment, not here: attendance dates
are derived from it at import time, and a value that can change at runtime would
give two sources of truth for which calendar day a punch belongs to. The API
exposes it read-only.
"""

from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel


class SalaryDayBasis(StrEnum):
    """What one day of pay is worth."""

    WORKING_DAYS = "WORKING_DAYS"  # month total / working days that month
    CALENDAR_DAYS = "CALENDAR_DAYS"  # month total / days in the month
    FIXED_26 = "FIXED_26"
    FIXED_30 = "FIXED_30"

SETTINGS_KEY = "organisation"


class OrgSettings(MongoModel):
    key: str = SETTINGS_KEY

    org_name: str = "Softus"

    # Monday is 0, Sunday is 6 — matches Shift.working_weekdays.
    week_start: int = Field(default=0, ge=0, le=6)

    # How far back a regularisation request may reach.
    correction_window_days: int = Field(default=7, ge=0, le=90)

    # Local hour after which an open punch session is auto-closed and the day is
    # marked as a missed punch-out.
    auto_close_hour: int = Field(default=23, ge=0, le=23)

    # Punch policy.
    geolocation_required: bool = True
    block_mobile_punch: bool = True
    min_punch_gap_seconds: int = Field(default=60, ge=0, le=3600)
    allow_multiple_sessions: bool = True

    # Task policy.
    tasks_required_daily: bool = False

    # Leave policy.
    leave_year_start_month: int = Field(default=1, ge=1, le=12)

    # Lateness with consequences: this many late arrivals in a month cost half
    # a day. Zero switches the policy off.
    late_arrivals_per_half_day: int = Field(default=0, ge=0, le=31)

    # How far back an approval may reach and still adjust pay.
    retro_approval_months: int = Field(default=3, ge=0, le=24)

    # Salary.
    salary_day_basis: SalaryDayBasis = SalaryDayBasis.WORKING_DAYS
    salary_deduct_absent: bool = True
    salary_deduct_unpaid_leave: bool = True
    salary_deduct_half_day: bool = True
    salary_deduct_missing_punch_out: bool = False

    # Which Saturdays of the month the office is open — 1 means the first
    # Saturday. The fifth is deliberately tri-state: often it is decided late.
    working_saturdays: list[int] = Field(default_factory=lambda: [2, 4])
    fifth_saturday_working: bool | None = None

    # Comp-off earned when a worked day is later swapped off.
    comp_off_expiry_days: int = Field(default=90, ge=0, le=730)

    support_email: str | None = None

    # The day attendance starts to count — the day the system went live, set by
    # the administrator's "reset". Days before it are not tracked: nobody is
    # absent, late or docked for a day the app was not running. The day itself
    # is a grace day; ordinary rules apply from the next one. ISO date or None.
    tracking_starts_on: str | None = None
