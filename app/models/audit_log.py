"""Immutable record of every administrative mutation.

Written by services, never updated or deleted. When someone asks why an
attendance record changed three months ago, this is the answer.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.core.datetime import utc_now
from app.models.base import MongoModel, PyObjectId


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    SUSPEND = "SUSPEND"
    ACTIVATE = "ACTIVATE"
    INVITE = "INVITE"
    ASSIGN = "ASSIGN"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ADJUST = "ADJUST"


class AuditEntity(StrEnum):
    USER = "USER"
    SHIFT = "SHIFT"
    HOLIDAY = "HOLIDAY"
    LEAVE_TYPE = "LEAVE_TYPE"
    LEAVE_REQUEST = "LEAVE_REQUEST"
    LEAVE_BALANCE = "LEAVE_BALANCE"
    ATTENDANCE = "ATTENDANCE"
    CORRECTION = "CORRECTION"
    TASK = "TASK"
    WORK_CALENDAR = "WORK_CALENDAR"
    COMP_OFF = "COMP_OFF"
    SALARY = "SALARY"
    SETTINGS = "SETTINGS"


class AuditLog(MongoModel):
    actor_id: PyObjectId | None = None
    actor_email: str | None = None
    action: AuditAction
    entity: AuditEntity
    entity_id: str | None = None
    entity_label: str | None = None

    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    ip: str | None = None
    at: datetime = Field(default_factory=utc_now)
