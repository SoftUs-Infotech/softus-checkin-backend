"""Leave type endpoints.

Employees need to read these to choose a leave type when applying, so listing is
open — but only to the types they may use. Everything else is admin-only.
"""

from fastapi import APIRouter, status

from app.core.exceptions import NotFoundError
from app.schemas.leave_type import LeaveTypeCreate, LeaveTypeOut, LeaveTypeUpdate
from app.services import leave_type as leave_type_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/leave-types", tags=["leave types"])


@router.get("", summary="List leave types")
async def list_leave_types(current_user: CurrentUser, include_inactive: bool = False) -> dict:
    types = await leave_type_service.list_leave_types(
        include_inactive=include_inactive, for_user=current_user
    )
    return success([LeaveTypeOut.from_document(item) for item in types])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a leave type")
async def create_leave_type(
    payload: LeaveTypeCreate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    created = await leave_type_service.create_leave_type(payload, actor)
    return success(LeaveTypeOut.from_document(created), message="Leave type created.")


@router.get("/{leave_type_id}", summary="Get one leave type")
async def get_leave_type(leave_type_id: str, current_user: CurrentUser) -> dict:
    leave_type = await leave_type_service.require_leave_type(leave_type_id)
    if not leave_type_service.visible_to(current_user, leave_type):
        raise NotFoundError("Leave type not found.", code="LEAVE_TYPE_NOT_FOUND")
    return success(LeaveTypeOut.from_document(leave_type))


@router.patch("/{leave_type_id}", summary="Update a leave type")
async def update_leave_type(
    leave_type_id: str, payload: LeaveTypeUpdate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    updated = await leave_type_service.update_leave_type(leave_type_id, payload, actor)
    return success(LeaveTypeOut.from_document(updated), message="Leave type updated.")


@router.delete("/{leave_type_id}", summary="Delete or deactivate a leave type")
async def delete_leave_type(
    leave_type_id: str, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    result = await leave_type_service.delete_leave_type(leave_type_id, actor)
    return success(result, message=result["message"])
