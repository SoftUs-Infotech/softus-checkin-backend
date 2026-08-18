"""Leave type API shapes."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.leave_type import AccrualPeriod

CODE_PATTERN = re.compile(r"^[A-Z0-9_]{2,20}$")
COLOR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class LeaveTypeBase(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    annual_quota: float = Field(default=0, ge=0, le=365)
    accrual: AccrualPeriod = AccrualPeriod.YEARLY
    is_paid: bool = True
    requires_approval: bool = True
    allow_half_day: bool = True
    carry_forward_max: float = Field(default=0, ge=0, le=365)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)
    applicable_after_days: int = Field(default=0, ge=0, le=730)
    applicable_departments: list[str] = Field(default_factory=list)
    color: str = "#2563eb"
    sort_order: int = 0
    description: str | None = Field(default=None, max_length=500)

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str) -> str:
        if not COLOR_PATTERN.match(value):
            raise ValueError("Colour must be a hex value such as #2563eb.")
        return value.lower()


class LeaveTypeCreate(LeaveTypeBase):
    code: str = Field(min_length=2, max_length=20)

    @field_validator("code")
    @classmethod
    def _normalise_code(cls, value: str) -> str:
        normalised = value.strip().upper().replace(" ", "_").replace("-", "_")
        if not CODE_PATTERN.match(normalised):
            raise ValueError("Code may use only letters, numbers and underscores (2–20 chars).")
        return normalised

    @model_validator(mode="after")
    def _carry_forward_within_quota(self) -> "LeaveTypeCreate":
        if self.carry_forward_max > self.annual_quota:
            raise ValueError(
                "Carry-forward cannot exceed the annual quota for this leave type."
            )
        return self


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    annual_quota: float | None = Field(default=None, ge=0, le=365)
    accrual: AccrualPeriod | None = None
    is_paid: bool | None = None
    requires_approval: bool | None = None
    allow_half_day: bool | None = None
    carry_forward_max: float | None = Field(default=None, ge=0, le=365)
    min_notice_days: int | None = Field(default=None, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)
    applicable_after_days: int | None = Field(default=None, ge=0, le=730)
    applicable_departments: list[str] | None = None
    color: str | None = None
    sort_order: int | None = None
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not COLOR_PATTERN.match(value):
            raise ValueError("Colour must be a hex value such as #2563eb.")
        return value.lower()


class LeaveTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str
    annual_quota: float
    accrual: AccrualPeriod
    is_paid: bool
    requires_approval: bool
    allow_half_day: bool
    carry_forward_max: float
    min_notice_days: int
    max_consecutive_days: int | None = None
    applicable_after_days: int
    applicable_departments: list[str] = Field(default_factory=list)
    color: str
    sort_order: int
    description: str | None = None
    is_active: bool = True

    @classmethod
    def from_document(cls, document: dict) -> "LeaveTypeOut":
        return cls.model_validate(document)
