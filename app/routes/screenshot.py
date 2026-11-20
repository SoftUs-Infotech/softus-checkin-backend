"""Work-from-home screenshots.

Uploading is for the employee working from home (the browser sends frames of
the screen they shared). Everything else — the summary, the gallery, deletion —
is for administrators.
"""

from datetime import date as date_type
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.core.config import settings
from app.core.datetime import org_today, to_utc, utc_now
from app.schemas.screenshot import (
    CaptureStoppedIn,
    ScreenshotDay,
    ScreenshotDelete,
    ScreenshotDeleteOut,
    ScreenshotOut,
    ScreenshotSummary,
    ScreenshotUploadOut,
)
from app.services import attendance as attendance_service
from app.services import screenshot as screenshot_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/screenshots", tags=["screenshots"])

#: A reported "last seen" further back than this is taken as this long ago.
STOPPED_AT_EARLIEST = timedelta(hours=2)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Upload a screenshot")
async def upload_screenshot(
    current_user: CurrentUser,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP")],
    width: Annotated[int | None, Form(ge=1, le=20000)] = None,
    height: Annotated[int | None, Form(ge=1, le=20000)] = None,
) -> dict:
    # One byte over the limit is enough to know it is too big, without reading
    # an arbitrarily large body into memory.
    data = await file.read(settings.SCREENSHOT_MAX_BYTES + 1)
    saved = await screenshot_service.save(current_user, data, width=width, height=height)
    return success(ScreenshotUploadOut(**saved), message="Screenshot saved.")


@router.post("/heartbeat", summary="The screen is still shared")
async def capture_heartbeat(current_user: CurrentUser) -> dict:
    seen_at = await screenshot_service.heartbeat(current_user)
    return success({"seen_at": seen_at})


@router.post("/stopped", summary="Screen sharing stopped — punch out")
async def capture_stopped(
    current_user: CurrentUser, payload: CaptureStoppedIn | None = None
) -> dict:
    """The browser or the desktop app reporting that the screen went away.

    The browser sends it when the employee presses "Stop sharing" and as the
    page closes or reloads; the desktop app when its window closes, when a
    setting it needs was turned off, when it could not see the screen, or when
    nobody used the keyboard or mouse for ten minutes (``IDLE``). Ends
    the open session at the last moment the screen was visible (never earlier
    than two hours ago); nothing open is fine.
    """
    screenshot_service.require_wfh(current_user)
    payload = payload or CaptureStoppedIn()
    now = utc_now()
    at = now
    if payload.at is not None:
        at = min(now, max(to_utc(payload.at), now - STOPPED_AT_EARLIEST))
    record = await attendance_service.end_session_for_capture_loss(
        current_user,
        at=at,
        reason=payload.reason,
        notify=payload.reason in ("SETTING_OFF", "SCREEN_NOT_VISIBLE", "IDLE"),
        setting=payload.setting,
        # Idle: how long nobody used the computer, from the last use to now.
        minutes=max(1, int((now - at).total_seconds() // 60)) if payload.reason == "IDLE" else None,
    )
    if record is None:
        return success(None, message="Nothing was open.")
    return success(
        attendance_service.to_day_out(record),
        message="Screen sharing stopped, so you were punched out.",
    )


@router.get("/summary", summary="Who works from home, and what is stored for them")
async def screenshot_summary(
    _: CurrentAdmin, date: date_type | None = None
) -> dict:
    data = await screenshot_service.summary(date or org_today())
    return success(ScreenshotSummary(**data))


@router.get("/days", summary="The days one person has screenshots on")
async def screenshot_days(
    _: CurrentAdmin, user_id: Annotated[str, Query(min_length=1)]
) -> dict:
    days = await screenshot_service.days_for(user_id)
    return success([ScreenshotDay(**day) for day in days])


@router.get("", summary="One person's screenshots for a day")
async def list_screenshots(
    _: CurrentAdmin,
    user_id: Annotated[str, Query(min_length=1)],
    date: date_type | None = None,
) -> dict:
    items = await screenshot_service.list_for(user_id, date or org_today())
    return success([ScreenshotOut(**item) for item in items])


@router.post("/delete", summary="Delete screenshots — selected, a person/day, or all")
async def delete_screenshots(
    payload: ScreenshotDelete, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    result = await screenshot_service.delete(payload, actor)
    deleted = result["deleted"]
    message = f"{deleted} screenshot{'s' if deleted != 1 else ''} deleted."
    return success(ScreenshotDeleteOut(**result), message=message)
