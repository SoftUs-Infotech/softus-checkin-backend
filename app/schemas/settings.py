"""Organisation settings API shapes."""

from pydantic import BaseModel, ConfigDict, Field


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
    support_email: str | None = None

    # Environment-driven, shown for reference and not editable through the API.
    timezone: str
    api_version: str = "0.1.0"

    @classmethod
    def from_document(cls, document: dict) -> "OrgSettingsOut":
        from app.core.config import settings

        data = dict(document)
        data["timezone"] = settings.ORG_TIMEZONE
        return cls.model_validate(data)
