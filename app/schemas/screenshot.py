"""Work-from-home screenshot shapes."""

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.models.user import UserStatus, WorkMode


class ScreenshotUploadOut(BaseModel):
    id: str
    captured_at: datetime


class ScreenshotOut(BaseModel):
    id: str
    user_id: str
    date: str
    captured_at: datetime
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    #: Short-lived. Absolute for S3; for local storage, a path under the API
    #: prefix (``/files?…``) that the web app joins onto its API address.
    url: str


class CaptureStoppedIn(BaseModel):
    """Why the screen stopped being shared, when the app or page can say.

    No body at all (the browser's "Stop sharing", a closed page) means
    ``SHARING_STOPPED`` now.
    """

    reason: Literal[
        "SETTING_OFF", "SCREEN_NOT_VISIBLE", "IDLE", "APP_CLOSED", "SHARING_STOPPED"
    ] = "SHARING_STOPPED"
    #: The last moment the screen was known to be visible (``IDLE``: the last
    #: use of the keyboard or mouse). Defaults to now; a moment before the
    #: session started counts as its start.
    at: AwareDatetime | None = None
    #: Which setting, for ``SETTING_OFF``: ``screen`` or ``input``.
    setting: Literal["screen", "input"] | None = None


class ScreenshotDay(BaseModel):
    date: str
    count: int
    size_bytes: int


class ScreenshotPerson(BaseModel):
    """One row of the work-from-home page: who, and what is stored for them."""

    id: str
    full_name: str
    email: str
    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None
    work_mode: WorkMode = WorkMode.OFFICE
    status: UserStatus | None = None
    #: The account was removed, but its screenshots are still taking space.
    removed: bool = False
    on_day: int = 0
    last_captured_at: datetime | None = None
    total_count: int = 0
    total_bytes: int = 0


class ScreenshotTotals(BaseModel):
    people_wfh: int
    on_day: int
    count: int
    size_bytes: int


class ScreenshotSummary(BaseModel):
    date: str
    interval_minutes: int
    totals: ScreenshotTotals
    people: list[ScreenshotPerson]


class ScreenshotDelete(BaseModel):
    """What to delete — exactly one kind of scope.

    * ``ids``: these screenshots;
    * ``user_id`` and/or ``date``: everything of that person, that day, or that
      person on that day;
    * ``everything``: every screenshot in the organisation.
    """

    ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    user_id: str | None = None
    date: date_type | None = None
    everything: bool = False

    @model_validator(mode="after")
    def _one_scope(self) -> "ScreenshotDelete":
        scopes = [
            self.ids is not None,
            self.user_id is not None or self.date is not None,
            self.everything,
        ]
        if sum(scopes) != 1:
            raise ValueError(
                "Say what to delete: a list of ids, a person and/or a date, or everything."
            )
        return self


class ScreenshotDeleteOut(BaseModel):
    deleted: int
    freed_bytes: int
    #: Files storage refused to delete. Their records are kept so a retry can
    #: find them again.
    failed: int = 0
