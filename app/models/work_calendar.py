"""Per-date rulings that the weekly pattern cannot express.

A shift says which weekdays are worked and the organisation says which Saturdays
of the month are open. Neither can say "the office is shut on the 14th" or "we
are taking the third Saturday off instead of the second". That is what these
documents are for, and they outrank both.

``date`` is a ``YYYY-MM-DD`` string in the organisation's timezone — the same key
attendance and holidays use, so a day lookup is a direct match.

A swap is a *pair*: two documents pointing at each other through
``swap_with_date``. Storing it as a pair rather than one "moved" row is what lets
the calendar answer both dates independently while keeping the month's
working-day count — and therefore the per-day rate — intact.
"""

from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel, PyObjectId


class DayKind(StrEnum):
    WORKING = "WORKING"
    OFF = "OFF"


class OverrideSource(StrEnum):
    """Why the ruling exists — a swap half must never be deleted on its own."""

    MANUAL = "MANUAL"
    SWAP = "SWAP"


class WorkCalendarOverride(MongoModel):
    date: str
    kind: DayKind
    reason: str = Field(max_length=300)

    source: OverrideSource = OverrideSource.MANUAL
    swap_with_date: str | None = None

    set_by: PyObjectId | None = None
    set_by_email: str | None = None


class FullDayCredit(MongoModel):
    """A date that counts as a full day for everybody who came in.

    For the day the office deliberately opens for three or four hours: the
    punches are still recorded, but nobody who turned up is short, half-day or
    late for it. Separate from the working/off ruling above because the two
    answer different questions — an ordinary open Saturday has no ruling at
    all, and must not need one to be marked.
    """

    date: str
    reason: str = Field(default="", max_length=300)

    set_by: PyObjectId | None = None
    set_by_email: str | None = None
