"""Organisation settings API shapes."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.models.settings import SalaryDayBasis


class OrgSettingsUpdate(BaseModel):
    org_name: str | None = Field(default=None, min_length=1, max_length=100)
    week_start: int | None = Field(default=None, ge=0, le=6)
    correction_window_days: int | None = Field(default=None, ge=0, le=90)
    auto_close_hour: int | None = Field(default=None, ge=0, le=23)
    geolocation_required: bool | None = None
    block_mobile_punch: bool | None = None
    min_punch_gap_seconds: int | None = Field(default=None, ge=0, le=3600)
    allow_multiple_sessions: bool | None = None
    tasks_required_daily: bool | None = None
    leave_year_start_month: int | None = Field(default=None, ge=1, le=12)

    late_arrivals_per_half_day: int | None = Field(default=None, ge=0, le=31)
    retro_approval_months: int | None = Field(default=None, ge=0, le=24)

    salary_day_basis: SalaryDayBasis | None = None
    salary_deduct_absent: bool | None = None
    salary_deduct_unpaid_leave: bool | None = None
    salary_deduct_half_day: bool | None = None
    salary_deduct_missing_punch_out: bool | None = None

    working_saturdays: list[int] | None = None
    fifth_saturday_working: bool | None = None

    comp_off_expiry_days: int | None = Field(default=None, ge=0, le=730)

    support_email: str | None = Field(default=None, max_length=120)


class OrgSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_name: str
    week_start: int
    correction_window_days: int
    auto_close_hour: int
    geolocation_required: bool
    block_mobile_punch: bool
    min_punch_gap_seconds: int
    allow_multiple_sessions: bool
    tasks_required_daily: bool
    leave_year_start_month: int

    late_arrivals_per_half_day: int = 0
    retro_approval_months: int = 3

    salary_day_basis: SalaryDayBasis = SalaryDayBasis.WORKING_DAYS
    salary_deduct_absent: bool = True
    salary_deduct_unpaid_leave: bool = True
    salary_deduct_half_day: bool = True
    salary_deduct_missing_punch_out: bool = False

    working_saturdays: list[int] = Field(default_factory=lambda: [2, 4])
    fifth_saturday_working: bool | None = None

    comp_off_expiry_days: int = 90

    support_email: str | None = None
    tracking_starts_on: str | None = None

    # Environment-driven, shown for reference and not editable through the API.
    timezone: str
    api_version: str = "0.1.0"

    @classmethod
    def from_document(cls, document: dict) -> "OrgSettingsOut":
        from app.core.config import settings

        data = dict(document)
        data["timezone"] = settings.ORG_TIMEZONE
        return cls.model_validate(data)


class TrackingStartUpdate(BaseModel):
    """The administrator's "reset": attendance counts from this day on."""

    #: Omitted means today in the organisation's timezone.
    starts_on: date | None = None
