"""Announcements, events and in-app notifications.

Announcements are broadcast; notifications are per employee. The dashboard's
right-hand rail reads announcements and upcoming events; the bell icon reads
notifications.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class AnnouncementType(StrEnum):
    ANNOUNCEMENT = "ANNOUNCEMENT"
    EVENT = "EVENT"


class Announcement(MongoModel):
    title: str = Field(max_length=200)
    body: str = Field(max_length=5000)
    type: AnnouncementType = AnnouncementType.ANNOUNCEMENT

    # Set for events; announcements leave it empty.
    event_date: str | None = None
    event_time: str | None = None
    location: str | None = Field(default=None, max_length=200)

    # Empty means everyone.
    audience_departments: list[str] = Field(default_factory=list)

    pinned: bool = False
    is_published: bool = True
    published_at: datetime | None = None

    author_id: PyObjectId | None = None
    author_name: str | None = None


class NotificationType(StrEnum):
    CORRECTION_DECIDED = "CORRECTION_DECIDED"
    LEAVE_DECIDED = "LEAVE_DECIDED"
    TASK_REVIEWED = "TASK_REVIEWED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    ATTENDANCE_ADJUSTED = "ATTENDANCE_ADJUSTED"
    LEAVE_CREDITED = "LEAVE_CREDITED"
    MISSING_PUNCH_OUT = "MISSING_PUNCH_OUT"
    GENERAL = "GENERAL"


class Notification(MongoModel):
    user_id: PyObjectId
    type: NotificationType = NotificationType.GENERAL
    title: str = Field(max_length=200)
    body: str | None = Field(default=None, max_length=1000)
    link: str | None = Field(default=None, max_length=300)
    read_at: datetime | None = None
