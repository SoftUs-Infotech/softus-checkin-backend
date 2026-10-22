"""Daily task endpoints."""

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.datetime import org_today
from app.models.task import TaskProgress, TaskStatus
from app.schemas.common import PaginatedData
from app.schemas.task import (
    TaskBulkDecision,
    TaskComment,
    TaskCreate,
    TaskOut,
    TaskProgressUpdate,
    TaskReview,
    TaskUpdate,
)
from app.services import task as task_service
from app.utils.deps import CurrentActor, CurrentApprover, CurrentUser, Pagination
from app.utils.response import success

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _page(items, pagination, total):
    return PaginatedData.build(
        [TaskOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Log a task")
async def create(payload: TaskCreate, current_user: CurrentUser, actor: CurrentActor) -> dict:
    created = await task_service.create_task(current_user, payload, actor)
    return success(TaskOut.from_document(created), message="Task logged.")


@router.get("/mine", summary="Your tasks")
async def mine(
    current_user: CurrentUser,
    pagination: Pagination,
    from_date: date_type | None = None,
    to_date: date_type | None = None,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    progress: TaskProgress | None = None,
) -> dict:
    items, total = await task_service.list_tasks(
        pagination,
        user_id=current_user["id"],
        from_date=from_date,
        to_date=to_date,
        status=task_status.value if task_status else None,
        progress=progress.value if progress else None,
    )
    return success(_page(items, pagination, total))


@router.get("/today", summary="What you logged today, plus what carried over")
async def today(current_user: CurrentUser) -> dict:
    tasks = await task_service.day_tasks(current_user["id"], org_today())
    return success([TaskOut.from_document(item) for item in tasks])


@router.get("/day", summary="One day's tasks, including everything carried in")
async def day(
    current_user: CurrentUser,
    date: date_type | None = None,
    user_id: str | None = None,
) -> dict:
    subject = await task_service.resolve_subject(current_user, user_id)
    tasks = await task_service.day_tasks(subject["id"], date or org_today())
    return success([TaskOut.from_document(item) for item in tasks])


@router.get("/calendar", summary="A month of one employee's tasks")
async def calendar(
    current_user: CurrentUser,
    user_id: str | None = None,
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
) -> dict:
    today_date = org_today()
    result = await task_service.month_explorer(
        current_user, user_id, year or today_date.year, month or today_date.month
    )
    return success(result)


@router.get("", summary="The task review queue")
async def queue(
    approver: CurrentApprover,
    pagination: Pagination,
    user_id: str | None = None,
    from_date: date_type | None = None,
    to_date: date_type | None = None,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    progress: TaskProgress | None = None,
) -> dict:
    items, total = await task_service.list_tasks(
        pagination,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        status=task_status.value if task_status else None,
        progress=progress.value if progress else None,
        approver=approver,
    )
    return success(_page(items, pagination, total))


@router.get("/pending-count", summary="How many tasks are waiting for review")
async def pending_count(approver: CurrentApprover) -> dict:
    return success({"pending": await task_service.pending_count(approver)})


@router.post("/bulk-decision", summary="Decide a batch of tasks in one call")
async def bulk_decision(
    payload: TaskBulkDecision, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    result = await task_service.bulk_review(payload, approver, actor)
    return success(result, message=f"{result.succeeded} of {result.requested} applied.")


@router.patch("/{task_id}", summary="Edit your task")
async def update(
    task_id: str, payload: TaskUpdate, current_user: CurrentUser, actor: CurrentActor
) -> dict:
    updated = await task_service.update_task(task_id, current_user, payload, actor)
    return success(TaskOut.from_document(updated), message="Task updated.")


@router.patch("/{task_id}/progress", summary="Move your task's progress")
async def progress(
    task_id: str,
    payload: TaskProgressUpdate,
    current_user: CurrentUser,
    actor: CurrentActor,
) -> dict:
    updated = await task_service.set_progress(task_id, current_user, payload, actor)
    return success(TaskOut.from_document(updated), message="Progress updated.")


@router.delete("/{task_id}", summary="Remove your task")
async def delete(task_id: str, current_user: CurrentUser, actor: CurrentActor) -> dict:
    await task_service.delete_task(task_id, current_user, actor)
    return success(message="Task removed.")


@router.post("/{task_id}/approve", summary="Approve a task")
async def approve(
    task_id: str, payload: TaskReview, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    updated = await task_service.review(
        task_id, TaskStatus.APPROVED, payload, approver, actor
    )
    return success(TaskOut.from_document(updated), message="Task approved.")


@router.post("/{task_id}/reject", summary="Reject a task")
async def reject(
    task_id: str, payload: TaskReview, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    updated = await task_service.review(
        task_id, TaskStatus.REJECTED, payload, approver, actor
    )
    return success(TaskOut.from_document(updated), message="Task rejected.")


@router.post("/{task_id}/request-changes", summary="Send a task back for changes")
async def request_changes(
    task_id: str, payload: TaskReview, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    updated = await task_service.review(
        task_id, TaskStatus.CHANGES_REQUESTED, payload, approver, actor
    )
    return success(TaskOut.from_document(updated), message="Changes requested.")


@router.post("/{task_id}/comment", summary="Add a note to the timeline")
async def comment(task_id: str, payload: TaskComment, current_user: CurrentUser) -> dict:
    updated = await task_service.comment(task_id, payload, current_user)
    return success(TaskOut.from_document(updated), message="Comment added.")
