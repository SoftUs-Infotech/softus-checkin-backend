"""Daily task logs.

An employee records what they worked on each day. An administrator reviews it,
and every review action appends to a timeline rather than overwriting the last
one — so the conversation about a task is preserved, not just its final state.

Two independent states live on the same document and are deliberately not merged.
``status`` is the administrator's word on the record — submitted, approved,
rejected, sent back. ``progress`` is the employee's word on the work itself, and
they own it: an approved record can still describe work that is not finished, and
a task marked done can still be waiting for review. Collapsing the two would
force one of those two people to lie.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.core.datetime import utc_now
from app.models.base import MongoModel, PyObjectId


class TaskStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class TaskProgress(StrEnum):
    TO_DO = "TO_DO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    BLOCKED = "BLOCKED"


class TaskAction(StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    RESUBMITTED = "RESUBMITTED"
    COMMENTED = "COMMENTED"
    PROGRESS_CHANGED = "PROGRESS_CHANGED"


class TimelineEntry(MongoModel):
    at: datetime = Field(default_factory=utc_now)
    actor_id: PyObjectId | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    action: TaskAction
    note: str | None = Field(default=None, max_length=1000)

    # Filled for anything that moves the task from one named state to another,
    # whichever of the two states it was. The admin reads one timeline, not two.
    from_state: str | None = None
    to_state: str | None = None


class Task(MongoModel):
    user_id: PyObjectId
    user_email: str | None = None
    user_name: str | None = None

    date: str  # YYYY-MM-DD in the organisation's timezone
    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=5000)

    planned_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    actual_minutes: int | None = Field(default=None, ge=0, le=24 * 60)

    project: str | None = Field(default=None, max_length=100)
    status: TaskStatus = TaskStatus.SUBMITTED

    progress: TaskProgress = TaskProgress.TO_DO
    progress_updated_at: datetime | None = None

    # The day the work was first logged. An unfinished task is never copied
    # forward — it keeps appearing on later days from this one row, so there is
    # exactly one history to read and nothing to reconcile.
    carried_from_date: str | None = None
    #: The organisation-local day progress reached DONE; cleared if it reopens.
    completed_on: str | None = None

    reviewer_id: PyObjectId | None = None
    reviewer_email: str | None = None
    reviewer_role: str | None = None
    reviewed_at: datetime | None = None

    timeline: list[TimelineEntry] = Field(default_factory=list)
