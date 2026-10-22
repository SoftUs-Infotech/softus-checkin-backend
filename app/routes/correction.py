"""Correction (regularisation) endpoints.

An employee sees only their own requests; an administrator sees the queue.
Filing one may be restricted to the office network — that gate lives in the
service, which is where the setting is read.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.models.correction import ApprovalStatus
from app.schemas.bulk import BulkDecision
from app.schemas.common import PaginatedData
from app.schemas.correction import CorrectionCreate, CorrectionDecision, CorrectionOut
from app.services import correction as correction_service
from app.utils.deps import CurrentActor, CurrentApprover, CurrentUser, Pagination
from app.utils.response import success

router = APIRouter(prefix="/corrections", tags=["corrections"])


def _page(items, pagination, total):
    return PaginatedData.build(
        [CorrectionOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Request a correction")
async def create(
    payload: CorrectionCreate, current_user: CurrentUser, actor: CurrentActor
) -> dict:
    created = await correction_service.create_correction(current_user, payload, actor)
    return success(CorrectionOut.from_document(created), message="Correction requested.")


@router.get("/mine", summary="Your correction requests")
async def mine(
    current_user: CurrentUser,
    pagination: Pagination,
    request_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> dict:
    items, total = await correction_service.list_corrections(
        pagination,
        user_id=current_user["id"],
        status=request_status.value if request_status else None,
    )
    return success(_page(items, pagination, total))


@router.get("", summary="The correction queue")
async def queue(
    approver: CurrentApprover,
    pagination: Pagination,
    request_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    user_id: str | None = None,
) -> dict:
    items, total = await correction_service.list_corrections(
        pagination,
        user_id=user_id,
        status=request_status.value if request_status else None,
        approver=approver,
    )
    return success(_page(items, pagination, total))


@router.get("/pending-count", summary="How many requests are waiting")
async def pending_count(approver: CurrentApprover) -> dict:
    return success({"pending": await correction_service.pending_count(approver)})


@router.post("/{correction_id}/approve", summary="Approve a correction")
async def approve(
    correction_id: str,
    payload: CorrectionDecision,
    approver: CurrentApprover,
    actor: CurrentActor,
) -> dict:
    updated = await correction_service.decide(correction_id, True, payload, approver, actor)
    return success(
        CorrectionOut.from_document(updated), message="Correction approved and applied."
    )


@router.post("/{correction_id}/reject", summary="Reject a correction")
async def reject(
    correction_id: str,
    payload: CorrectionDecision,
    approver: CurrentApprover,
    actor: CurrentActor,
) -> dict:
    updated = await correction_service.decide(
        correction_id, False, payload, approver, actor
    )
    return success(CorrectionOut.from_document(updated), message="Correction rejected.")


@router.post("/bulk-decision", summary="One decision across many corrections")
async def bulk_decision(
    payload: BulkDecision, approver: CurrentApprover, actor: CurrentActor
) -> dict:
    result = await correction_service.bulk_decide(payload, approver, actor)
    verb = "approved" if payload.approve else "rejected"
    return success(
        result,
        message=f"{result.succeeded} corrections {verb}, {result.failed} could not be.",
    )


@router.post("/{correction_id}/cancel", summary="Withdraw your own request")
async def cancel(
    correction_id: str, current_user: CurrentUser, actor: CurrentActor
) -> dict:
    updated = await correction_service.cancel(correction_id, current_user, actor)
    return success(CorrectionOut.from_document(updated), message="Request withdrawn.")
