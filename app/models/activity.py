"""One minute of a work-from-home employee's activity, as the desktop app measured it.

Only counts are kept — never which keys were pressed, nothing typed, no extra
images. The app sends one row per minute while the person is punched in; the
admin page adds the minutes up per screenshot window.
"""

from datetime import datetime

from app.models.base import MongoModel


class ActivityMinute(MongoModel):
    user_id: str
    #: The organisation-local day, ``YYYY-MM-DD``.
    date: str
    #: The start of the minute, UTC.
    minute: datetime
    #: Seconds in this minute with keyboard or mouse input anywhere on the computer.
    active_seconds: int = 0
    keys: int = 0
    clicks: int = 0
    scrolls: int = 0
    #: How far the pointer travelled, in screen pixels.
    mouse_distance: int = 0
    #: Small, local comparisons of the screen with itself a few seconds earlier.
    screen_samples: int = 0
    screen_changes: int = 0
    #: Seconds the screen was locked.
    locked_seconds: int = 0
    #: Seconds with input from a software device rather than hardware — a
    #: remote-control tool or a "mouse jiggler" program (Linux only).
    virtual_seconds: int = 0
    app_version: str | None = None
