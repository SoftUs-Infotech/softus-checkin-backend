"""Announcements, events, notifications, and the dashboard summary."""

from fastapi import APIRouter, Query, status

from app.schemas.announcement import (
    AnnouncementCreate,
    AnnouncementOut,
    AnnouncementUpdate,
    NotificationOut,
)
from app.schemas.common import PaginatedData
from app.schemas.holiday import HolidayOut
from app.services import announcement as announcement_service
from app.services import correction as correction_service
from app.services import holiday as holiday_service
from app.services import leave as leave_service
from app.services import notification as notification_service
from app.services import task as task_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser, Pagination
from app.utils.response import success

router = APIRouter(tags=["announcements"])


# --------------------------------------------------------------------------- #
# announcements and events
# --------------------------------------------------------------------------- #
@router.get("/announcements", summary="Announcements for you")
async def list_announcements(
    current_user: CurrentUser,
    include_unpublished: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    is_admin = current_user.get("role") == "ADMIN"
    items = await announcement_service.list_announcements(
        department=None if is_admin else current_user.get("department"),
        include_unpublished=include_unpublished and is_admin,
        limit=limit,
    )
    return success([AnnouncementOut.from_document(item) for item in items])


@router.get("/announcements/upcoming-events", summary="Upcoming events")
async def upcoming_events(_: CurrentUser, limit: int = Query(default=5, ge=1, le=20)) -> dict:
    items = await announcement_service.upcoming_events(limit)
    return success([AnnouncementOut.from_document(item) for item in items])


@router.post(
    "/announcements", status_code=status.HTTP_201_CREATED, summary="Post an announcement"
)
async def create_announcement(
    payload: AnnouncementCreate, admin: CurrentAdmin, actor: CurrentActor
) -> dict:
    created = await announcement_service.create_announcement(payload, admin, actor)
    return success(AnnouncementOut.from_document(created), message="Announcement posted.")


@router.patch("/announcements/{announcement_id}", summary="Edit an announcement")
async def update_announcement(
    announcement_id: str,
    payload: AnnouncementUpdate,
    _: CurrentAdmin,
    actor: CurrentActor,
) -> dict:
    updated = await announcement_service.update_announcement(announcement_id, payload, actor)
    return success(AnnouncementOut.from_document(updated), message="Announcement updated.")


@router.delete("/announcements/{announcement_id}", summary="Remove an announcement")
async def delete_announcement(
    announcement_id: str, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    await announcement_service.delete_announcement(announcement_id, actor)
    return success(message="Announcement removed.")


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
@router.get("/notifications", summary="Your notifications")
async def list_notifications(
    current_user: CurrentUser, pagination: Pagination, unread_only: bool = False
) -> dict:
    items, total = await notification_service.list_notifications(
        current_user["id"], pagination, unread_only=unread_only
    )
    page = PaginatedData.build(
        [NotificationOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
    return success(page)


@router.get("/notifications/unread-count", summary="Unread badge count")
async def unread_count(current_user: CurrentUser) -> dict:
    return success({"unread": await notification_service.unread_count(current_user["id"])})


@router.post("/notifications/{notification_id}/read", summary="Mark one as read")
async def mark_read(notification_id: str, current_user: CurrentUser) -> dict:
    changed = await notification_service.mark_read(current_user["id"], notification_id)
    return success({"updated": changed}, message="Marked as read.")


@router.post("/notifications/read-all", summary="Mark everything as read")
async def mark_all_read(current_user: CurrentUser) -> dict:
    changed = await notification_service.mark_all_read(current_user["id"])
    return success({"updated": changed}, message="All notifications marked as read.")


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@router.get("/dashboard/summary", summary="Everything the dashboard rail needs")
async def dashboard_summary(current_user: CurrentUser) -> dict:
    """One call for the right-hand rail, rather than six.

    Administrators additionally receive the pending-approval counts that drive
    the sidebar badges.
    """
    is_admin = current_user.get("role") == "ADMIN"

    payload = {
        "announcements": [
            AnnouncementOut.from_document(item)
            for item in await announcement_service.list_announcements(
                department=None if is_admin else current_user.get("department"), limit=5
            )
        ],
        "upcoming_events": [
            AnnouncementOut.from_document(item)
            for item in await announcement_service.upcoming_events(5)
        ],
        "upcoming_holidays": [
            HolidayOut.from_document(item) for item in await holiday_service.upcoming(5)
        ],
        "leave_balances": await leave_service.list_balances(
            current_user["id"], for_user=current_user
        ),
        "on_leave_today": await leave_service.on_leave_today(),
        "unread_notifications": await notification_service.unread_count(current_user["id"]),
    }

    # A manager decides for their own reports, so the badges belong to them as
    # much as to an administrator — scoped, and the services do the scoping.
    if current_user.get("role") in ("ADMIN", "MANAGER"):
        payload["pending"] = {
            "corrections": await correction_service.pending_count(approver=current_user),
            "leave": await leave_service.pending_count(approver=current_user),
            "tasks": await task_service.pending_count(approver=current_user),
        }

    return success(payload)
