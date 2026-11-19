"""Work-from-home activity shapes."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class ActivityMinuteIn(BaseModel):
    minute: AwareDatetime
    active_seconds: int = Field(ge=0, le=60)
    keys: int = Field(default=0, ge=0, le=5000)
    clicks: int = Field(default=0, ge=0, le=2000)
    scrolls: int = Field(default=0, ge=0, le=5000)
    mouse_distance: int = Field(default=0, ge=0, le=50_000_000)
    screen_samples: int = Field(default=0, ge=0, le=60)
    screen_changes: int = Field(default=0, ge=0, le=60)
    locked_seconds: int = Field(default=0, ge=0, le=60)
    virtual_seconds: int = Field(default=0, ge=0, le=60)

    @model_validator(mode="after")
    def _changes_within_samples(self) -> "ActivityMinuteIn":
        if self.screen_changes > self.screen_samples:
            raise ValueError("screen_changes cannot exceed screen_samples")
        return self


class ActivityUpload(BaseModel):
    minutes: list[ActivityMinuteIn] = Field(min_length=1, max_length=720)


class ActivityUploadOut(BaseModel):
    accepted: int
    #: Minutes too old or in the future, which were ignored.
    ignored: int


class ActivityFigures(BaseModel):
    tracked_minutes: int = 0
    active_seconds: int = 0
    keys: int = 0
    clicks: int = 0
    scrolls: int = 0
    mouse_distance: int = 0
    screen_samples: int = 0
    screen_changes: int = 0
    locked_seconds: int = 0
    virtual_seconds: int = 0


class ActivityWindow(ActivityFigures):
    start: datetime
    end: datetime
    #: Share of the tracked time with keyboard or mouse input, 0–100.
    active_percent: int = 0
    #: ``high`` / ``medium`` / ``low``, by ``active_percent``.
    level: str = "low"
    #: Patterns worth a look: ``idle``, ``locked``, ``mouse_only`` (input but no
    #: keys, clicks or screen change — a mouse jiggler) and ``screen_only``
    #: (the screen changes with no input — a video left playing) and
    #: ``virtual_input`` (much of the input came from software, not hardware).
    flags: list[str] = Field(default_factory=list)


class ActivityDay(BaseModel):
    date: str
    interval_minutes: int
    totals: ActivityFigures
    active_percent: int = 0
    windows: list[ActivityWindow]


class AppEventIn(BaseModel):
    """One line of the app log, as the desktop app reports it."""

    at: AwareDatetime
    kind: str = Field(min_length=1, max_length=40)
    details: dict[str, str | int | bool | None] = Field(default_factory=dict, max_length=10)


class AppEventsUpload(BaseModel):
    events: list[AppEventIn] = Field(min_length=1, max_length=50)


class AppEventsUploadOut(BaseModel):
    accepted: int
    ignored: int


class AppLogEntry(BaseModel):
    at: datetime
    #: Set for a span, such as the screen being locked from ``at`` until this.
    until: datetime | None = None
    kind: str
    #: ``app`` (reported by the desktop app) or ``server`` (decided or seen by the server).
    source: str
    message: str
