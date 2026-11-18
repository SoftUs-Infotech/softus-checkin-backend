"""Work-from-home activity: the desktop app sends it, administrators read it."""

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.datetime import org_today
from app.schemas.activity import ActivityDay, ActivityUpload, ActivityUploadOut
from app.services import activity as activity_service
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
