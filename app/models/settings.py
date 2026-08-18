"""Organisation settings — a single document holding attendance policy.

The timezone deliberately lives in the environment, not here: attendance dates
are derived from it at import time, and a value that can change at runtime would
give two sources of truth for which calendar day a punch belongs to. The API
exposes it read-only.
"""

from pydantic import Field

from app.models.base import MongoModel

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

    support_email: str | None = None
