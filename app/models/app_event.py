"""One line in a work-from-home employee's app log — seen only by administrators.

The desktop app reports what happened on the computer (opened, a setting was
turned off, the screen could not be seen, a popup was shown); the server adds
what it decided (punched in, punched out automatically, the app was updated).
The admin reads them together, in time order, next to the screenshots.

Nothing here is shown to the employee.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.models.base import MongoModel


class AppEventKind(StrEnum):
    # Reported by the desktop app.
    APP_OPENED = "APP_OPENED"
    APP_CLOSED = "APP_CLOSED"
    SETTING_CHANGED = "SETTING_CHANGED"
    SCREEN_NOT_VISIBLE = "SCREEN_NOT_VISIBLE"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    POPUP_SHOWN = "POPUP_SHOWN"
    # Recorded by the server.
    PUNCHED_IN = "PUNCHED_IN"
    PUNCHED_OUT = "PUNCHED_OUT"
    AUTO_PUNCHED_OUT = "AUTO_PUNCHED_OUT"
    APP_UPDATED = "APP_UPDATED"


#: What the app may report. The rest are the server's to record.
APP_REPORTED = frozenset(
    {
        AppEventKind.APP_OPENED,
        AppEventKind.APP_CLOSED,
        AppEventKind.SETTING_CHANGED,
        AppEventKind.SCREEN_NOT_VISIBLE,
        AppEventKind.SCREENSHOT_FAILED,
        AppEventKind.POPUP_SHOWN,
    }
)


class AppEventSource(StrEnum):
    APP = "app"
    SERVER = "server"


class AppEvent(MongoModel):
    user_id: str
    #: The organisation-local day, ``YYYY-MM-DD``.
    date: str
    #: When it happened (for an automatic punch-out: the punch-out time).
    at: datetime
    kind: AppEventKind
    source: AppEventSource
    #: A few short values; what each kind carries is up to that kind.
    details: dict[str, Any] = Field(default_factory=dict)
    app_version: str | None = None
    os: str | None = None
    os_version: str | None = None
