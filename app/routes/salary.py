"""Compensation. Administrators only — no employee-facing endpoint exists here.

Every route on this router depends on ``CurrentAdmin``. There is deliberately no
"my salary" endpoint and no salary field on any employee-facing schema: an
employee must not be able to read their own figure, including by guessing a URL.
Adding a route here without the admin dependency is the one mistake that breaks
that promise, so keep the dependency on the route itself rather than trusting a
router-level default.
"""

from fastapi import APIRouter, Query, status

from app.schemas.salary import (
    AdjustmentCreate,
    OrgProjectionOut,
    ProjectionOut,
    SalaryCreate,
    SalaryOut,
)
from app.services import salary as salary_service
from app.utils.deps import CurrentActor, CurrentAdmin
from app.utils.response import success

router = APIRouter(prefix="/salary", tags=["salary"])

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@router.get("/employees/{user_id}", summary="An employee's salary history")
async def list_salaries(user_id: str, _: CurrentAdmin) -> dict:
    records = await salary_service.list_salaries(user_id)
    return success([SalaryOut.model_validate(record) for record in records])


@router.post(
    "/employees/{user_id}",
    status_code=status.HTTP_201_CREATED,
    summary="Record a salary from a date",
)
async def set_salary(
    user_id: str, payload: SalaryCreate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    record = await salary_service.set_salary(user_id, payload, actor)
    return success(SalaryOut.model_validate(record), message="Salary recorded.")


@router.post(
    "/employees/{user_id}/adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Post an adjustment in days to a month",
)
async def create_adjustment(
    user_id: str, payload: AdjustmentCreate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    adjustment = await salary_service.record_adjustment(
        user_id=user_id,
        apply_month=payload.apply_month,
        days=payload.days,
        reason=payload.reason,
        kind=payload.kind,
        source_month=payload.source_month,
        source_date=payload.source_date.isoformat() if payload.source_date else None,
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
        actor=actor,
    )
    return success(adjustment, message="Adjustment recorded.")


@router.get(
    "/employees/{user_id}/projection",
    summary="One employee's projected month",
)
async def project_employee(
    user_id: str,
    _: CurrentAdmin,
    month: str = Query(pattern=MONTH_PATTERN, description="YYYY-MM"),
) -> dict:
    projection = await salary_service.project_for_user(user_id, month)
    return success(ProjectionOut.model_validate(projection))


@router.get("/projection", summary="The whole organisation's projected month")
async def project_organisation(
    _: CurrentAdmin,
    month: str = Query(pattern=MONTH_PATTERN, description="YYYY-MM"),
) -> dict:
    projection = await salary_service.project_organisation(month)
    return success(OrgProjectionOut.model_validate(projection))
