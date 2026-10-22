"""Leave endpoints: balances, applying, approving."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.datetime import org_today
from app.models.correction import ApprovalStatus
from app.schemas.bulk import BulkDecision
from app.schemas.common import PaginatedData
from app.schemas.leave import (
    LeaveApply,
    LeaveBalanceAdjust,
    LeaveBalanceOut,
    LeaveDecision,
    LeaveRequestOut,
)
from app.services import leave as leave_service
from app.services import user as user_service
from app.utils.deps import (
    CurrentActor,
    CurrentAdmin,
    CurrentApprover,
    CurrentUser,
    Pagination,
)
from app.utils.response import success

router = APIRouter(prefix="/leave", tags=["leave"])


def _page(items, pagination, total):
    return PaginatedData.build(
        [LeaveRequestOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


# --------------------------------------------------------------------------- #
# balances
# --------------------------------------------------------------------------- #
@router.get("/balances", summary="Your leave balances")
async def my_balances(
    current_user: CurrentUser, year: int | None = Query(default=None, ge=2000, le=2100)
) -> dict:
    balances = await leave_service.list_balances(current_user["id"], year)
    return success([LeaveBalanceOut(**item) for item in balances])


@router.get("/balances/{user_id}", summary="An employee's leave balances")
async def employee_balances(
    user_id: str, _: CurrentAdmin, year: int | None = Query(default=None, ge=2000, le=2100)
) -> dict:
    await user_service.require_user(user_id)
    balances = await leave_service.list_balances(user_id, year)
    return success([LeaveBalanceOut(**item) for item in balances])


@router.patch("/balances/{user_id}/{leave_type_id}", summary="Adjust a balance")
async def adjust_balance(
    user_id: str,
    leave_type_id: str,
    payload: LeaveBalanceAdjust,
    _: CurrentAdmin,
    actor: CurrentActor,
) -> dict:
    await user_service.require_user(user_id)
    updated = await leave_service.adjust_balance(user_id, leave_type_id, payload, actor)
    return success(updated, message="Balance adjusted.")


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
@router.post("", status_code=status.HTTP_201_CREATED, summary="Apply for leave")
async def apply(payload: LeaveApply, current_user: CurrentUser, actor: CurrentActor) -> dict:
    created = await leave_service.apply_for_leave(current_user, payload, actor)
    return success(LeaveRequestOut.from_document(created), message="Leave requested.")


@router.get("/mine", summary="Your leave requests")
async def mine(
    current_user: CurrentUser,
    pagination: Pagination,
    request_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> dict:
    items, total = await leave_service.list_requests(
        pagination,
        user_id=current_user["id"],
        status=request_status.value if request_status else None,
    )
    return success(_page(items, pagination, total))


@router.get("", summary="The leave queue")
async def queue(
    approver: CurrentApprover,
    pagination: Pagination,
    user_id: str | None = None,
    request_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> dict:
    items, total = await leave_service.list_requests(
        pagination,
        user_id=user_id,
        status=request_status.value if request_status else None,
        approver=approver,
    )
    return success(_page(items, pagination, total))


@router.get("/pending-count", summary="How many requests are waiting")
async def pending_count(approver: CurrentApprover) -> dict:
    return success({"pending": await leave_service.pending_count(approver)})


@router.get("/on-leave-today", summary="Who is away today")
async def on_leave_today(_: CurrentUser) -> dict:
    return success(
        {"date": org_today().isoformat(), "employees": await leave_service.on_leave_today()}
    )


@router.post("/{request_id}/approve", summary="Approve a leave request")
async def approve(
    request_id: str, payload: LeaveDecision, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    updated = await leave_service.decide(request_id, True, payload, approver, actor)
    return success(LeaveRequestOut.from_document(updated), message="Leave approved.")


@router.post("/{request_id}/reject", summary="Reject a leave request")
async def reject(
    request_id: str, payload: LeaveDecision, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    updated = await leave_service.decide(request_id, False, payload, approver, actor)
    return success(LeaveRequestOut.from_document(updated), message="Leave rejected.")


@router.post("/bulk-decision", summary="One decision across many leave requests")
async def bulk_decision(
    payload: BulkDecision, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    result = await leave_service.bulk_decide(payload, approver, actor)
    verb = "approved" if payload.approve else "rejected"
    return success(
        result,
        message=f"{result.succeeded} leave requests {verb}, {result.failed} could not be.",
    )


@router.post("/{request_id}/cancel", summary="Withdraw your own request")
async def cancel(request_id: str, current_user: CurrentUser, actor: CurrentActor) -> dict:
    updated = await leave_service.cancel(request_id, current_user, actor)
    return success(LeaveRequestOut.from_document(updated), message="Leave withdrawn.")
