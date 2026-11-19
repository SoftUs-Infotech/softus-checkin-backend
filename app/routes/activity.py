"""Work-from-home activity: the desktop app sends it, administrators read it."""

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.datetime import org_today
from app.schemas.activity import (
    ActivityDay,
    ActivityUpload,
    ActivityUploadOut,
    AppEventsUpload,
    AppEventsUploadOut,
    AppLogEntry,
)
from app.services import activity as activity_service
from app.services import app_log
from app.utils.client import Client
from app.utils.deps import CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/activity", tags=["activity"])


@router.post("", summary="Minutes of activity from the desktop app")
async def upload_activity(
    payload: ActivityUpload, current_user: CurrentUser, client: Client
) -> dict:
    result = await activity_service.save(current_user, payload, client)
    return success(ActivityUploadOut(**result))


@router.get("/day", summary="One person's activity for a day, per screenshot window")
async def activity_day(
    _: CurrentAdmin,
    user_id: Annotated[str, Query(min_length=1)],
    date: date_type | None = None,
) -> dict:
    return success(ActivityDay(**await activity_service.day(user_id, date or org_today())))


@router.post("/events", summary="Lines for the app log, from the desktop app")
async def report_app_events(
    payload: AppEventsUpload, current_user: CurrentUser, client: Client
) -> dict:
    result = await app_log.save_reported(
        current_user, [event.model_dump() for event in payload.events], client
    )
    return success(AppEventsUploadOut(**result))


@router.get("/log", summary="One person's app log for a day — administrators only")
async def app_log_day(
    _: CurrentAdmin,
    user_id: Annotated[str, Query(min_length=1)],
    date: date_type | None = None,
) -> dict:
    entries = await app_log.log_for(user_id, date or org_today())
    return success([AppLogEntry(**entry) for entry in entries])
