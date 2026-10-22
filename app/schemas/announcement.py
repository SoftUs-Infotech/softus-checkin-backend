"""Announcement and notification API shapes."""

from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.announcement import AnnouncementType, NotificationType


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    type: AnnouncementType = AnnouncementType.ANNOUNCEMENT
    event_date: date_type | None = None
    event_time: str | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=200)
    audience_departments: list[str] = Field(default_factory=list)
    pinned: bool = False
    is_published: bool = True

    @model_validator(mode="after")
    def _an_event_needs_a_date(self) -> "AnnouncementCreate":
        if self.type is AnnouncementType.EVENT and self.event_date is None:
            raise ValueError("An event needs a date.")
        return self


class AnnouncementUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    event_date: date_type | None = None
    event_time: str | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=200)
    audience_departments: list[str] | None = None
    pinned: bool | None = None
    is_published: bool | None = None


class AnnouncementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    body: str
    type: AnnouncementType
    event_date: str | None = None
    event_time: str | None = None
    location: str | None = None
    audience_departments: list[str] = Field(default_factory=list)
    pinned: bool = False
    is_published: bool = True
    published_at: datetime | None = None
    author_name: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict) -> "AnnouncementOut":
        return cls.model_validate(document)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: NotificationType
    title: str
    body: str | None = None
    link: str | None = None
    read_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict) -> "NotificationOut":
        return cls.model_validate(document)
