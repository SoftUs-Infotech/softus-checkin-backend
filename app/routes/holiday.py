"""Holiday endpoints. Every employee can read the calendar; admins maintain it."""

from datetime import date

from fastapi import APIRouter, Query, status

from app.schemas.holiday import (
    HolidayBulkCreate,
    HolidayCreate,
    HolidayOut,
    HolidayQuery,
    HolidayUpdate,
)
from app.services import holiday as holiday_service
from app.services import leave as leave_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/holidays", tags=["holidays"])


@router.get("", summary="List holidays")
async def list_holidays(
    _: CurrentUser,
    year: int | None = Query(default=None, ge=2000, le=2100),
    from_date: date | None = None,
    to_date: date | None = None,
    include_inactive: bool = False,
) -> dict:
    query = HolidayQuery(
        year=year, from_date=from_date, to_date=to_date, include_inactive=include_inactive
    )
    holidays = await holiday_service.list_holidays(query)
    return success([HolidayOut.from_document(item) for item in holidays])


@router.get("/upcoming", summary="The next few holidays")
async def upcoming(_: CurrentUser, limit: int = Query(default=5, ge=1, le=20)) -> dict:
    holidays = await holiday_service.upcoming(limit)
    return success([HolidayOut.from_document(item) for item in holidays])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a holiday")
async def create_holiday(payload: HolidayCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    created = await holiday_service.create_holiday(payload, actor)
    message = "Holiday added."
    if returned := await leave_service.give_back_days_off(payload.date, payload.date):
        message += f" Leave on that day returned for {returned} request(s)."
    return success(HolidayOut.from_document(created), message=message)


@router.post("/bulk", summary="Add many holidays at once")
async def bulk_create(payload: HolidayBulkCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    result = await holiday_service.bulk_create(payload, actor)
    if payload.holidays:
        dates = [item.date for item in payload.holidays]
        await leave_service.give_back_days_off(min(dates), max(dates))
    message = f"{result['created']} holiday(s) added."
    if result["skipped"]:
        message += f" {result['skipped']} skipped as duplicates."
    return success(result, message=message)


@router.patch("/{holiday_id}", summary="Update a holiday")
async def update_holiday(
    holiday_id: str, payload: HolidayUpdate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    updated = await holiday_service.update_holiday(holiday_id, payload, actor)
    moved_to = date.fromisoformat(updated["date"])
    await leave_service.give_back_days_off(moved_to, moved_to)
    return success(HolidayOut.from_document(updated), message="Holiday updated.")


@router.delete("/{holiday_id}", summary="Remove a holiday")
async def delete_holiday(holiday_id: str, _: CurrentAdmin, actor: CurrentActor) -> dict:
    await holiday_service.delete_holiday(holiday_id, actor)
    return success(message="Holiday removed.")
