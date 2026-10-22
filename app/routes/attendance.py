"""Attendance endpoints.

``DesktopOnly`` guards punch-in and punch-out and nothing else — an employee can
still read their calendar from a phone, they just cannot punch from one. The
office-network and ``can_punch`` gates live in the service, next to the rules
they protect.
"""

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.core.datetime import org_today, parse_date_key
from app.models.attendance import AttendanceStatus
from app.schemas.attendance import (
    AttendanceAdjust,
    AttendanceQuery,
    PunchRequestBody,
    RegisterQuery,
    RegisterSort,
    SortDirection,
    TodayOut,
)
from app.services import attendance as attendance_service
from app.services import user as user_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.device import DesktopOnly, classify
from app.utils.network import client_ip
from app.utils.response import success

router = APIRouter(prefix="/attendance", tags=["attendance"])


# --------------------------------------------------------------------------- #
# punching — computers only
# --------------------------------------------------------------------------- #
@router.post("/punch-in", summary="Punch in (desktop only)")
async def punch_in(
    payload: PunchRequestBody,
    request: Request,
    current_user: CurrentUser,
    device: DesktopOnly,
) -> dict:
    record = await attendance_service.punch_in(
        current_user,
        payload,
        ip=client_ip(request),
        user_agent=device.user_agent,
        device_class=device.device_class.value,
        platform=device.platform,
    )
    return success(attendance_service.to_day_out(record), message="Punched in.")


@router.post("/punch-out", summary="Punch out (desktop only)")
async def punch_out(
    payload: PunchRequestBody,
    request: Request,
    current_user: CurrentUser,
    device: DesktopOnly,
) -> dict:
    record = await attendance_service.punch_out(
        current_user,
        payload,
        ip=client_ip(request),
        user_agent=device.user_agent,
        device_class=device.device_class.value,
        platform=device.platform,
    )
    return success(attendance_service.to_day_out(record), message="Punched out.")


# --------------------------------------------------------------------------- #
# reading — any device
# --------------------------------------------------------------------------- #
@router.get("/today", summary="Today's state for the dashboard widget")
async def today(request: Request, current_user: CurrentUser) -> dict:
    verdict = classify(request)
    payload = await attendance_service.get_today(
        current_user, device_class=verdict.device_class.value
    )
    return success(TodayOut(**payload))


@router.get("/calendar", summary="A month of attendance")
async def calendar(
    current_user: CurrentUser,
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    user_id: str | None = None,
) -> dict:
    today_date = org_today()
    target = current_user
    if user_id and user_id != current_user["id"]:
        # Only an administrator may look at somebody else's calendar.
        from app.core.exceptions import PermissionDeniedError
        from app.models.user import UserRole

        if current_user.get("role") != UserRole.ADMIN:
            raise PermissionDeniedError("You may only view your own attendance.")
        target = await user_service.require_user(user_id)

    result = await attendance_service.month_calendar(
        target, year or today_date.year, month or today_date.month
    )
    return success(result)


@router.get("/history", summary="A range of recorded days")
async def history(
    current_user: CurrentUser,
    from_date: date_type | None = None,
    to_date: date_type | None = None,
) -> dict:
    query = AttendanceQuery(from_date=from_date, to_date=to_date)
    days = await attendance_service.list_attendance(
        query, requesting_user_id=current_user["id"]
    )
    return success(days)


# --------------------------------------------------------------------------- #
# administration
# --------------------------------------------------------------------------- #
@router.get("/register", summary="Every employee's status for a day or a span")
async def register(
    _: CurrentAdmin,
    day: date_type | None = None,
    from_date: date_type | None = None,
    to_date: date_type | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    department: Annotated[str | None, Query(max_length=100)] = None,
    attendance_status: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
    exceptions_only: bool = False,
    sort: RegisterSort | None = None,
    direction: SortDirection = SortDirection.ASC,
) -> dict:
    rows, meta = await attendance_service.register(
        RegisterQuery(
            day=day,
            from_date=from_date,
            to_date=to_date,
            search=search,
            department=department,
            status=attendance_status,
            exceptions_only=exceptions_only,
            sort=sort,
            direction=direction,
        )
    )
    return success(rows, meta=meta.model_dump())


@router.get("/employee/{user_id}", summary="One employee's recorded days")
async def employee_history(
    user_id: str,
    _: CurrentAdmin,
    from_date: date_type | None = None,
    to_date: date_type | None = None,
) -> dict:
    await user_service.require_user(user_id)
    query = AttendanceQuery(from_date=from_date, to_date=to_date, user_id=user_id)
    return success(await attendance_service.list_attendance(query))


@router.put("/employee/{user_id}/{day}", summary="Adjust an employee's day")
async def adjust(
    user_id: str,
    day: str,
    payload: AttendanceAdjust,
    _: CurrentAdmin,
    actor: CurrentActor,
) -> dict:
    record = await attendance_service.adjust_day(
        user_id, parse_date_key(day), payload, actor
    )
    return success(attendance_service.to_day_out(record), message="Attendance updated.")
