"""Leave type API shapes."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.leave_type import (
    AccrualFrequency,
    AccrualPolicy,
    AccrualTiming,
    annual_quota_for,
    resolve_accrual_policy,
)

CODE_PATTERN = re.compile(r"^[A-Z0-9_]{2,20}$")
COLOR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class AccrualPolicyIn(BaseModel):
    frequency: AccrualFrequency = AccrualFrequency.YEARLY
    amount_per_cycle: float = Field(default=0, ge=0, le=365)
    credit_at: AccrualTiming = AccrualTiming.START
    lapse_at_year_end: bool = True


class AccrualPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    frequency: AccrualFrequency
    amount_per_cycle: float
    credit_at: AccrualTiming
    lapse_at_year_end: bool


#: A bare frequency string is still accepted so callers written against the
#: original two-value accrual keep working; it is translated the same way a
#: stored legacy document is.
AccrualIn = AccrualPolicyIn | AccrualFrequency


def _as_policy(value: AccrualIn | None, annual_quota: float) -> AccrualPolicy:
    if isinstance(value, AccrualPolicyIn):
        return AccrualPolicy.model_validate(value.model_dump())
    return resolve_accrual_policy({"accrual": value, "annual_quota": annual_quota})


class LeaveTypeBase(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    annual_quota: float = Field(default=0, ge=0, le=365)
    accrual: AccrualIn | None = None
    is_paid: bool = True
    requires_approval: bool = True
    allow_half_day: bool = True
    allow_negative_balance: bool = False
    carry_forward_max: float = Field(default=0, ge=0, le=365)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)
    applicable_after_days: int = Field(default=0, ge=0, le=730)
    applicable_departments: list[str] = Field(default_factory=list)
    visible_to_employees: bool = True
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
        # A policy given without a quota implies one, and carry-forward is
        # judged against that rather than against a zero nobody entered.
        policy = _as_policy(self.accrual, self.annual_quota)
        self.accrual = AccrualPolicyIn.model_validate(policy.model_dump())
        self.annual_quota = self.annual_quota or annual_quota_for(policy)
        if self.carry_forward_max > self.annual_quota:
            raise ValueError(
                "Carry-forward cannot exceed the annual quota for this leave type."
            )
        return self


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    annual_quota: float | None = Field(default=None, ge=0, le=365)
    accrual: AccrualIn | None = None
    is_paid: bool | None = None
    requires_approval: bool | None = None
    allow_half_day: bool | None = None
    allow_negative_balance: bool | None = None
    carry_forward_max: float | None = Field(default=None, ge=0, le=365)
    min_notice_days: int | None = Field(default=None, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)
    applicable_after_days: int | None = Field(default=None, ge=0, le=730)
    applicable_departments: list[str] | None = None
    visible_to_employees: bool | None = None
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
    accrual: AccrualPolicyOut
    is_paid: bool
    requires_approval: bool
    allow_half_day: bool
    allow_negative_balance: bool = False
    carry_forward_max: float
    min_notice_days: int
    max_consecutive_days: int | None = None
    applicable_after_days: int
    applicable_departments: list[str] = Field(default_factory=list)
    visible_to_employees: bool = True
    color: str
    sort_order: int
    description: str | None = None
    is_active: bool = True

    @classmethod
    def from_document(cls, document: dict) -> "LeaveTypeOut":
        # Legacy documents store a frequency string where the policy now lives.
        return cls.model_validate(
            {**document, "accrual": resolve_accrual_policy(document)}
        )
