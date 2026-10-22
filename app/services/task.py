"""Daily tasks: the review timeline, employee-owned progress and carry-forward.

Every state change appends a timeline entry. Editing an approved task is not
allowed — an employee who needs to change it asks for changes to be reopened,
which keeps the approval meaningful.

Carry-forward is a *reading* rule, not a writing one. An unfinished task is never
duplicated onto the next day; it keeps its original ``date`` and every later day
that asks for its tasks picks it up again because it is still open. One row, one
history — which is what makes ``days_open`` mean anything, and what stops a task
from being approved twice under two different ids.
"""

from datetime import date, timedelta
from typing import Any

from bson import ObjectId

from app.core.datetime import date_key, month_range, org_today, parse_date_key, utc_now
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import NotificationType
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.task import Task, TaskAction, TaskProgress, TaskStatus, TimelineEntry
from app.schemas.common import PaginationParams
from app.schemas.task import (
    TaskBulkDecision,
    TaskBulkDecisionOut,
    TaskBulkResult,
    TaskCalendarDay,
    TaskCalendarOut,
    TaskComment,
    TaskCreate,
    TaskMonthSummary,
    TaskProgressUpdate,
    TaskReview,
    TaskUpdate,
    TaskWeekSummary,
)
from app.services import audit
from app.services import notification as notification_service
from app.services import user as user_service
from app.services.audit import Actor

logger = get_logger(__name__)

#: Statuses an employee may still edit. An approved task is settled.
EDITABLE_STATUSES = {TaskStatus.SUBMITTED.value, TaskStatus.CHANGES_REQUESTED.value}

#: How long a task may stay open before the explorer flags it as stale.
#: Two calendar weeks: long enough that a genuinely large piece of work is not
#: nagged about on its second morning, short enough that anything still open
#: after two sprints' worth of days is something nobody is actually working on.
STALE_AFTER_DAYS = 14

#: Tasks fetched for one employee-day. Nobody logs a hundred tasks in a day, and
#: an unbounded cursor here would be a carry-forward bug waiting to happen.
DAY_TASK_LIMIT = 200


def _tasks():
    return get_collection(Collections.TASKS)


def _display_name(user: dict[str, Any]) -> str:
    return (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("email", "")
    )


def _entry(
    user: dict[str, Any],
    action: TaskAction,
    note: str | None = None,
    *,
    from_state: str | None = None,
    to_state: str | None = None,
) -> dict[str, Any]:
    return TimelineEntry(
        actor_id=user.get("id"),
        actor_name=_display_name(user),
        actor_role=user.get("role"),
        action=action,
        note=note,
        from_state=from_state,
        to_state=to_state,
    ).to_mongo()


def _origin(task: dict[str, Any]) -> str:
    """The day the work was first logged, tolerating rows written before v2."""
    return task.get("carried_from_date") or task["date"]


def _was_open_on(task: dict[str, Any], day: str) -> bool:
    """Was this task still open at the end of ``day``?

    A task closed on a day was open *that* day — that is the day it got finished,
    so it belongs on that day's list rather than vanishing from it.
    """
    completed = task.get("completed_on")
    return completed is None or completed >= day


def decorate(task: dict[str, Any], *, on: date | None = None) -> dict[str, Any]:
    """Add the fields derived from where a task sits relative to a given day."""
    day = date_key(on or org_today())
    origin = _origin(task)
    closed = task.get("completed_on")
    # A finished task stops ageing on the day it was finished; an open one is
    # measured against the day being looked at rather than against right now, so
    # a historical view reports what the administrator would have seen then.
    reference = min(closed, day) if closed else day
    task = dict(task)
    task["carried_from_date"] = origin
    task["days_open"] = max(0, (parse_date_key(reference) - parse_date_key(origin)).days)
    task["is_stale"] = closed is None and task["days_open"] >= STALE_AFTER_DAYS
    task["is_carried_forward"] = origin < day and _was_open_on(task, day)
    return task


async def require_task(task_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(task_id):
        raise NotFoundError("Task not found.", code="TASK_NOT_FOUND")
    document = await _tasks().find_one({"_id": ObjectId(task_id), "is_deleted": False})
    if document is None:
        raise NotFoundError("Task not found.", code="TASK_NOT_FOUND")
    return decorate(serialize(document))


async def _require_own_task(task_id: str, user: dict[str, Any]) -> dict[str, Any]:
    task = await require_task(task_id)
    if task["user_id"] != user["id"]:
        # Not "forbidden" — an employee should not learn that the task exists.
        raise NotFoundError("Task not found.", code="TASK_NOT_FOUND")
    return task


async def create_task(
    user: dict[str, Any], payload: TaskCreate, actor: Actor
) -> dict[str, Any]:
    if payload.date > org_today():
        raise ValidationError(
            "A task cannot be logged for a future date.", code="FUTURE_DATE"
        )

    day = date_key(payload.date)
    progress = payload.progress or TaskProgress.TO_DO
    task = Task(
        user_id=user["id"],
        user_email=user.get("email"),
        user_name=_display_name(user),
        date=day,
        title=payload.title,
        description=payload.description,
        planned_minutes=payload.planned_minutes,
        actual_minutes=payload.actual_minutes,
        project=payload.project,
        progress=progress,
        carried_from_date=day,
        completed_on=day if progress is TaskProgress.DONE else None,
    )
    document = task.to_mongo()
    document["timeline"] = [_entry(user, TaskAction.CREATED, to_state=progress.value)]
    result = await _tasks().insert_one(document)

    created = await require_task(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.TASK,
        entity_id=created["id"],
        entity_label=f"{user.get('email')} {created['date']}",
        after={"title": created["title"], "date": created["date"]},
    )
    return created


async def update_task(
    task_id: str, user: dict[str, Any], payload: TaskUpdate, actor: Actor
) -> dict[str, Any]:
    task = await _require_own_task(task_id, user)
    if task["status"] not in EDITABLE_STATUSES:
        raise ConflictError(
            "An approved task cannot be edited. Ask your administrator to reopen it.",
            code="TASK_NOT_EDITABLE",
        )

    changes = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return task

    was_reopened = task["status"] == TaskStatus.CHANGES_REQUESTED.value
    changes["status"] = TaskStatus.SUBMITTED.value
    changes["updated_at"] = utc_now()

    await _tasks().update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": changes,
            "$push": {
                "timeline": _entry(
                    user,
                    TaskAction.RESUBMITTED if was_reopened else TaskAction.UPDATED,
                    from_state=task["status"],
                    to_state=TaskStatus.SUBMITTED.value,
                )
            },
        },
    )

    updated = await require_task(task_id)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.TASK,
        entity_id=task_id,
        entity_label=f"{user.get('email')} {task['date']}",
        before=task,
        after=updated,
    )
    return updated


async def set_progress(
    task_id: str, user: dict[str, Any], payload: TaskProgressUpdate, actor: Actor
) -> dict[str, Any]:
    """Move the employee's own progress marker.

    Deliberately unrestricted by date and by review status: work continues past
    the day it was logged on, and an approved *record* of Monday's work says
    nothing about whether the work itself finished on Friday. Only the owner may
    do this — an administrator reporting progress on somebody else's behalf is
    exactly the fiction this field exists to prevent.
    """
    task = await _require_own_task(task_id, user)
    current = task.get("progress") or TaskProgress.TO_DO.value
    if current == payload.progress.value:
        raise ConflictError(
            f"This task is already {payload.progress.value.lower().replace('_', ' ')}.",
            code="TASK_PROGRESS_UNCHANGED",
        )

    now = utc_now()
    today = date_key(org_today())
    changes: dict[str, Any] = {
        "progress": payload.progress.value,
        "progress_updated_at": now,
        "updated_at": now,
        # Reopening clears the closing date, so the task carries forward again and
        # keeps ageing from its original day instead of quietly resetting to zero.
        "completed_on": today if payload.progress is TaskProgress.DONE else None,
    }

    await _tasks().update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": changes,
            "$push": {
                "timeline": _entry(
                    user,
                    TaskAction.PROGRESS_CHANGED,
                    payload.note,
                    from_state=current,
                    to_state=payload.progress.value,
                )
            },
        },
    )

    updated = await require_task(task_id)
    await audit.record(
        actor,
        AuditAction.UPDATE,
        AuditEntity.TASK,
        entity_id=task_id,
        entity_label=f"{task.get('user_email')} {task['date']}",
        before={"progress": current},
        after={"progress": payload.progress.value, "note": payload.note},
    )
    return updated


async def delete_task(task_id: str, user: dict[str, Any], actor: Actor) -> None:
    task = await _require_own_task(task_id, user)
    if task["status"] == TaskStatus.APPROVED.value:
        raise ConflictError(
            "An approved task cannot be deleted.", code="TASK_NOT_DELETABLE"
        )

    await _tasks().update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"is_deleted": True, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.TASK,
        entity_id=task_id,
        entity_label=f"{user.get('email')} {task['date']}",
        before={"title": task["title"], "date": task["date"]},
    )


async def list_tasks(
    pagination: PaginationParams,
    *,
    user_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status: str | None = None,
    progress: str | None = None,
    approver: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    query: dict[str, Any] = {"is_deleted": False}
    if approver is not None:
        query.update(await user_service.approval_scope(approver, user_id))
    elif user_id:
        query["user_id"] = user_id
    if status:
        query["status"] = status
    if progress:
        query["progress"] = progress
    if from_date or to_date:
        window: dict[str, str] = {}
        if from_date:
            window["$gte"] = date_key(from_date)
        if to_date:
            window["$lte"] = date_key(to_date)
        query["date"] = window

    total = await _tasks().count_documents(query)
    cursor = (
        _tasks()
        .find(query)
        .sort([("date", -1), ("created_at", -1)])
        .skip(pagination.skip)
        .limit(pagination.limit)
    )
    items = [
        decorate(serialize(document))
        for document in await cursor.to_list(length=pagination.limit)
    ]
    return items, total


async def day_tasks(user_id: str, day: date) -> list[dict[str, Any]]:
    """That day's own tasks, plus everything still open that rolled into it.

    Carried rows sort first: an employee opening today's list should be met by
    the work that has been waiting, not by whatever they typed last.
    """
    key = date_key(day)
    cursor = (
        _tasks()
        .find(
            {
                "user_id": user_id,
                "is_deleted": False,
                "date": {"$lte": key},
                "$or": [
                    {"date": key},
                    {"completed_on": None},
                    {"completed_on": {"$gte": key}},
                ],
            }
        )
        .sort([("date", 1), ("created_at", 1)])
    )
    documents = await cursor.to_list(length=DAY_TASK_LIMIT)
    return [
        decorate(serialize(document), on=day)
        for document in documents
        if document["date"] == key or _was_open_on(document, key)
    ]


async def pending_count(approver: dict[str, Any] | None = None) -> int:
    # No approver means the whole organisation — see ``correction.pending_count``.
    query: dict[str, Any] = {"status": TaskStatus.SUBMITTED.value, "is_deleted": False}
    if approver is not None:
        query.update(await user_service.approval_scope(approver, None))
    return await _tasks().count_documents(query)


async def review(
    task_id: str,
    decision: TaskStatus,
    payload: TaskReview,
    reviewer: dict[str, Any],
    actor: Actor,
) -> dict[str, Any]:
    task = await require_task(task_id)
    await user_service.require_approval_authority(
        reviewer,
        task["user_id"],
        own_message="You cannot review your own task.",
        own_code="CANNOT_REVIEW_OWN_TASK",
    )
    if task["status"] == decision.value:
        raise ConflictError(
            f"This task is already {decision.value.lower().replace('_', ' ')}.",
            code="TASK_ALREADY_IN_STATE",
        )

    action = {
        TaskStatus.APPROVED: TaskAction.APPROVED,
        TaskStatus.REJECTED: TaskAction.REJECTED,
        TaskStatus.CHANGES_REQUESTED: TaskAction.CHANGES_REQUESTED,
    }[decision]

    now = utc_now()
    await _tasks().update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": {
                "status": decision.value,
                "reviewer_id": reviewer["id"],
                "reviewer_email": reviewer.get("email"),
                "reviewer_role": reviewer["role"],
                "reviewed_at": now,
                "updated_at": now,
            },
            "$push": {
                "timeline": _entry(
                    reviewer,
                    action,
                    payload.note,
                    from_state=task["status"],
                    to_state=decision.value,
                )
            },
        },
    )

    updated = await require_task(task_id)
    await audit.record(
        actor,
        AuditAction.APPROVE if decision is TaskStatus.APPROVED else AuditAction.REJECT,
        AuditEntity.TASK,
        entity_id=task_id,
        entity_label=f"{task.get('user_email')} {task['date']}",
        before={"status": task["status"]},
        after={"status": decision.value, "note": payload.note},
    )
    await notification_service.notify(
        task["user_id"],
        type=NotificationType.TASK_REVIEWED,
        title=f"Your task '{task['title'][:60]}' was "
        f"{decision.value.lower().replace('_', ' ')}",
        body=payload.note,
        link="/tasks",
    )
    logger.info("Task %s moved to %s by %s", task_id, decision, reviewer.get("email"))
    return updated


async def bulk_review(
    payload: TaskBulkDecision, reviewer: dict[str, Any], actor: Actor
) -> TaskBulkDecisionOut:
    """One decision and one note applied to many tasks.

    A bad id is reported against that id and nothing else: an administrator
    clearing a morning's queue should not lose twenty good decisions because the
    twenty-first task was deleted in another tab.
    """
    review_payload = TaskReview(note=payload.note)
    results: list[TaskBulkResult] = []

    for task_id in dict.fromkeys(payload.task_ids):
        try:
            await review(task_id, payload.decision, review_payload, reviewer, actor)
        except AppError as error:
            results.append(
                TaskBulkResult(
                    task_id=task_id,
                    success=False,
                    error_code=error.code,
                    message=error.message,
                )
            )
        else:
            results.append(TaskBulkResult(task_id=task_id, success=True))

    succeeded = sum(1 for result in results if result.success)
    logger.info(
        "Bulk %s over %d tasks by %s: %d succeeded",
        payload.decision,
        len(results),
        reviewer.get("email"),
        succeeded,
    )
    return TaskBulkDecisionOut(
        decision=payload.decision,
        requested=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )


async def comment(
    task_id: str, payload: TaskComment, user: dict[str, Any]
) -> dict[str, Any]:
    """Add a note without changing the status — the timeline your brief asked for."""
    task = await require_task(task_id)
    # Anyone entitled to review the task may annotate it; to everybody else it
    # does not exist, rather than being forbidden.
    if task["user_id"] != user["id"] and not await user_service.may_approve_for(
        user, task["user_id"]
    ):
        raise NotFoundError("Task not found.", code="TASK_NOT_FOUND")

    await _tasks().update_one(
        {"_id": ObjectId(task_id)},
        {
            "$push": {"timeline": _entry(user, TaskAction.COMMENTED, payload.note)},
            "$set": {"updated_at": utc_now()},
        },
    )
    return await require_task(task_id)


async def resolve_subject(
    current_user: dict[str, Any], user_id: str | None
) -> dict[str, Any]:
    """Whose tasks the caller is asking about, and whether they may ask."""
    if not user_id or user_id == current_user["id"]:
        return current_user
    if not await user_service.may_approve_for(current_user, user_id):
        raise PermissionDeniedError("You may only view your own tasks.")
    return await user_service.require_user(user_id)


def month_weeks(first: date, last: date) -> list[tuple[date, date]]:
    """The month cut into calendar weeks, clipped at the month boundaries."""
    weeks: list[tuple[date, date]] = []
    start = first
    while start <= last:
        end = min(start + timedelta(days=6 - start.weekday()), last)
        weeks.append((start, end))
        start = end + timedelta(days=1)
    return weeks


async def month_explorer(
    current_user: dict[str, Any], user_id: str | None, year: int, month: int
) -> TaskCalendarOut:
    """A month of one employee's tasks: day counts and a week-by-week summary."""
    subject = await resolve_subject(current_user, user_id)
    first, last = month_range(year, month)
    first_key, last_key = date_key(first), date_key(last)

    # Anything logged on or before the month's last day and not already closed
    # before the month began can show up somewhere in this grid.
    documents = (
        await _tasks()
        .find(
            {
                "user_id": subject["id"],
                "is_deleted": False,
                "date": {"$lte": last_key},
                "$or": [
                    {"date": {"$gte": first_key}},
                    {"completed_on": None},
                    {"completed_on": {"$gte": first_key}},
                ],
            }
        )
        .to_list(length=None)
    )
    tasks = [serialize(document) for document in documents]

    days: list[TaskCalendarDay] = []
    for offset in range((last - first).days + 1):
        key = date_key(first + timedelta(days=offset))
        own = [task for task in tasks if task["date"] == key]
        carried = [
            task for task in tasks if task["date"] < key and _was_open_on(task, key)
        ]
        visible = own + carried
        days.append(
            TaskCalendarDay(
                date=key,
                total=len(visible),
                done=sum(1 for task in visible if task.get("completed_on") == key),
                carried_in=len(carried),
            )
        )

    weeks: list[TaskWeekSummary] = []
    for number, (week_start, week_end) in enumerate(month_weeks(first, last), start=1):
        start_key, end_key = date_key(week_start), date_key(week_end)
        weeks.append(
            TaskWeekSummary(
                week=number,
                from_date=start_key,
                to_date=end_key,
                completed=sum(
                    1
                    for task in tasks
                    if task.get("completed_on")
                    and start_key <= task["completed_on"] <= end_key
                ),
                in_progress=sum(
                    1
                    for task in tasks
                    if task["date"] <= end_key
                    and (
                        task.get("completed_on") is None
                        or task["completed_on"] > end_key
                    )
                ),
                carried_forward=sum(
                    1
                    for task in tasks
                    if task["date"] < start_key and _was_open_on(task, start_key)
                ),
            )
        )

    logged = [task for task in tasks if first_key <= task["date"] <= last_key]
    # Staleness is judged as at the end of the month, or today for the month in
    # progress — a task logged yesterday is not stale because December is far off.
    as_at = min(last, org_today())
    decorated = [decorate(task, on=as_at) for task in logged]
    return TaskCalendarOut(
        user_id=subject["id"],
        user_name=_display_name(subject),
        year=year,
        month=month,
        from_date=first_key,
        to_date=last_key,
        stale_after_days=STALE_AFTER_DAYS,
        days=days,
        weeks=weeks,
        summary=TaskMonthSummary(
            logged=len(logged),
            completed=sum(1 for task in logged if task.get("completed_on")),
            still_open=sum(1 for task in logged if not task.get("completed_on")),
            stale=sum(1 for task in decorated if task["is_stale"]),
        ),
    )
