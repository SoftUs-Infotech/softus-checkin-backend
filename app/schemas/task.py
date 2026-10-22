"""Daily task API shapes."""

from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.task import TaskAction, TaskProgress, TaskStatus

#: The decisions an administrator may hand down. SUBMITTED is where a task
#: starts, not somewhere a reviewer can send it.
DECISIONS = {TaskStatus.APPROVED, TaskStatus.REJECTED, TaskStatus.CHANGES_REQUESTED}


class TaskCreate(BaseModel):
    date: date_type
    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    planned_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    actual_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    project: str | None = Field(default=None, max_length=100)
    progress: TaskProgress | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    planned_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    actual_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    project: str | None = Field(default=None, max_length=100)


class TaskProgressUpdate(BaseModel):
    progress: TaskProgress
    note: str | None = Field(default=None, max_length=1000)


class TaskReview(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class TaskBulkDecision(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=200)
    decision: TaskStatus
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("decision")
    @classmethod
    def _must_be_a_decision(cls, value: TaskStatus) -> TaskStatus:
        if value not in DECISIONS:
            raise ValueError("A bulk decision must approve, reject or request changes.")
        return value


class TaskBulkResult(BaseModel):
    task_id: str
    success: bool
    error_code: str | None = None
    message: str | None = None


class TaskBulkDecisionOut(BaseModel):
    decision: TaskStatus
    requested: int
    succeeded: int
    failed: int
    results: list[TaskBulkResult]


class TaskComment(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class TimelineEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: datetime
    actor_id: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    action: TaskAction
    note: str | None = None
    from_state: str | None = None
    to_state: str | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    user_email: str | None = None
    user_name: str | None = None
    date: str
    title: str
    description: str | None = None
    planned_minutes: int | None = None
    actual_minutes: int | None = None
    project: str | None = None
    status: TaskStatus
    progress: TaskProgress = TaskProgress.TO_DO
    progress_updated_at: datetime | None = None
    carried_from_date: str | None = None
    completed_on: str | None = None
    days_open: int = 0
    is_stale: bool = False
    is_carried_forward: bool = False
    reviewer_email: str | None = None
    reviewer_role: str | None = None
    reviewed_at: datetime | None = None
    timeline: list[TimelineEntryOut] = Field(default_factory=list)
    #: When the employee submitted the task — the admin sees the clock time, not
    #: just the day it belongs to.
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict) -> "TaskOut":
        return cls.model_validate(document)


class TaskDaySummary(BaseModel):
    date: str
    task_count: int
    total_actual_minutes: int
    statuses: dict[str, int]


class TaskCalendarDay(BaseModel):
    date: str
    total: int
    done: int
    carried_in: int


class TaskWeekSummary(BaseModel):
    week: int
    from_date: str
    to_date: str
    completed: int
    in_progress: int
    carried_forward: int


class TaskMonthSummary(BaseModel):
    logged: int
    completed: int
    still_open: int
    stale: int


class TaskCalendarOut(BaseModel):
    user_id: str
    user_name: str | None = None
    year: int
    month: int
    from_date: str
    to_date: str
    stale_after_days: int
    days: list[TaskCalendarDay]
    weeks: list[TaskWeekSummary]
    summary: TaskMonthSummary
