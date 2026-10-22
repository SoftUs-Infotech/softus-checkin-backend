"""Salary API shapes — administrator-facing only.

Nothing here is embedded in a user, attendance or dashboard schema. An employee
response must never gain a salary field by inheritance, so these models stand
alone deliberately.
"""

from datetime import date as date_type

from pydantic import BaseModel, ConfigDict, Field

from app.models.salary import SalaryAdjustmentKind


class SalaryCreate(BaseModel):
    monthly_ctc: float = Field(ge=0, le=1_000_000_000)
    effective_from: date_type
    note: str | None = Field(default=None, max_length=300)


class SalaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    monthly_ctc: float
    effective_from: str
    note: str | None = None
    set_by_email: str | None = None


class AdjustmentCreate(BaseModel):
    apply_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    days: float = Field(ge=-31, le=31)
    reason: str = Field(min_length=3, max_length=300)
    kind: SalaryAdjustmentKind = SalaryAdjustmentKind.MANUAL
    source_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source_date: date_type | None = None
    reference_type: str | None = Field(default=None, max_length=50)
    reference_id: str | None = Field(default=None, max_length=64)


class DeductionLine(BaseModel):
    kind: str
    label: str
    days: float
    amount: float


class AdjustmentLine(BaseModel):
    kind: str
    days: float
    amount: float
    reason: str | None = None
    source_month: str | None = None
    source_date: str | None = None
    reference_type: str | None = None
    reference_id: str | None = None


class ProjectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    has_salary_on_record: bool

    month: str
    monthly_ctc: float
    effective_from: str | None = None
    day_basis: str
    day_rate: float

    counts: dict[str, float]
    deductions: list[DeductionLine] = Field(default_factory=list)
    adjustments: list[AdjustmentLine] = Field(default_factory=list)

    total_deduction: float
    total_adjustment: float
    projected_net: float


class OrgProjectionOut(BaseModel):
    month: str
    employees: list[ProjectionOut]
    total_projected_net: float
    missing_salary_count: int
