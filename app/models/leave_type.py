"""Leave categories.

Entirely admin-defined: introducing a new kind of leave is a row here, never a
code change. Existing requests keep pointing at the type they were made under.

Accrual is a stored policy rather than a code branch — how often days are
credited, how many per cycle, whether the credit lands at the start or the end
of it, and what survives the year end. "Half a day of sick leave a month" is a
row, not a release.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.base import MongoModel


class AccrualFrequency(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class AccrualTiming(StrEnum):
    START = "START"  # credited on the first day of the cycle
    END = "END"  # credited only once the cycle has finished


#: Cycles a calendar year holds, per frequency. Leave years are calendar years
#: here because balances are keyed by year everywhere else.
CYCLES_PER_YEAR: dict[str, int] = {
    AccrualFrequency.MONTHLY.value: 12,
    AccrualFrequency.QUARTERLY.value: 4,
    AccrualFrequency.YEARLY.value: 1,
}


class AccrualPolicy(BaseModel):
    """How a leave type earns days."""

    model_config = ConfigDict(use_enum_values=True)

    frequency: AccrualFrequency = AccrualFrequency.YEARLY
    amount_per_cycle: float = Field(default=0, ge=0, le=365)
    credit_at: AccrualTiming = AccrualTiming.START

    # Unused days die with the year unless carry_forward_max rescues some of
    # them. Turning this off lets a balance grow indefinitely, which is what an
    # earned-leave-style type wants.
    lapse_at_year_end: bool = True


def annual_quota_for(policy: AccrualPolicy) -> float:
    """What a full year under this policy is worth."""
    return round(policy.amount_per_cycle * CYCLES_PER_YEAR[str(policy.frequency)], 2)


def resolve_accrual_policy(document: Mapping[str, Any]) -> AccrualPolicy:
    """The one place old and new accrual configuration are reconciled.

    Types created before accrual became a policy store a bare frequency string
    next to ``annual_quota``: the whole quota at the start of the year, or a
    twelfth of it each month. Translating on read rather than migrating the
    collection means a document nobody has touched since can never crash the
    accrual job, and an admin editing such a type upgrades it in place.
    """
    raw = document.get("accrual")
    if isinstance(raw, AccrualPolicy):
        return raw
    if isinstance(raw, Mapping):
        return AccrualPolicy.model_validate(dict(raw))

    frequency = (
        AccrualFrequency(raw)
        if isinstance(raw, str) and raw in AccrualFrequency.__members__
        else AccrualFrequency.YEARLY
    )
    quota = float(document.get("annual_quota") or 0)
    return AccrualPolicy(
        frequency=frequency,
        amount_per_cycle=round(quota / CYCLES_PER_YEAR[frequency.value], 4),
    )


class LeaveType(MongoModel):
    name: str
    code: str

    #: A year's worth of entitlement, kept in step with the accrual policy. It
    #: is what carry-forward is validated against and what the UI shows.
    annual_quota: float = Field(default=0, ge=0, le=365)
    accrual: AccrualPolicy = Field(default_factory=AccrualPolicy)

    is_paid: bool = True
    requires_approval: bool = True
    allow_half_day: bool = True

    # Going overdrawn is refused unless a type explicitly permits it.
    allow_negative_balance: bool = False

    carry_forward_max: float = Field(default=0, ge=0, le=365)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=365)

    # Days after joining before an employee may use — or earn — this type.
    applicable_after_days: int = Field(default=0, ge=0, le=730)

    # Empty means every department.
    applicable_departments: list[str] = Field(default_factory=list)

    # A hidden type is the administrators' to record: employees neither see it
    # nor apply for it. It still accrues, so showing it again later shows a
    # balance that is already right.
    visible_to_employees: bool = True

    color: str = "#2563eb"
    sort_order: int = 0
    is_active: bool = True
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_accrual(cls, data: Any) -> Any:
        if isinstance(data, dict) and not isinstance(
            data.get("accrual"), AccrualPolicy | dict
        ):
            return {**data, "accrual": resolve_accrual_policy(data)}
        return data

    @model_validator(mode="after")
    def _quota_follows_the_policy(self) -> "LeaveType":
        if not self.annual_quota:
            self.annual_quota = annual_quota_for(self.accrual)
        return self
