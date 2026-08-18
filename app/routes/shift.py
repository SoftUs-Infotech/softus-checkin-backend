"""Shift endpoints. Reading is open to any employee; writing is admin-only."""

from fastapi import APIRouter, status

from app.schemas.shift import ShiftAssign, ShiftCreate, ShiftOut, ShiftUpdate
from app.services import shift as shift_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/shifts", tags=["shifts"])


@router.get("", summary="List shifts")
async def list_shifts(_: CurrentUser, include_inactive: bool = False) -> dict:
    shifts = await shift_service.list_shifts(include_inactive=include_inactive)
    counts = {shift["id"]: await shift_service.count_assigned(shift["id"]) for shift in shifts}
    return success(
        [ShiftOut.from_document(shift, assigned_count=counts[shift["id"]]) for shift in shifts]
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a shift")
async def create_shift(payload: ShiftCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    created = await shift_service.create_shift(payload, actor)
    return success(ShiftOut.from_document(created), message="Shift created.")


@router.get("/{shift_id}", summary="Get one shift")
async def get_shift(shift_id: str, _: CurrentUser) -> dict:
    shift = await shift_service.require_shift(shift_id)
    assigned = await shift_service.count_assigned(shift_id)
    return success(ShiftOut.from_document(shift, assigned_count=assigned))


@router.patch("/{shift_id}", summary="Update a shift")
async def update_shift(
    shift_id: str, payload: ShiftUpdate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    updated = await shift_service.update_shift(shift_id, payload, actor)
    return success(ShiftOut.from_document(updated), message="Shift updated.")


@router.delete("/{shift_id}", summary="Delete a shift")
async def delete_shift(shift_id: str, _: CurrentAdmin, actor: CurrentActor) -> dict:
    await shift_service.delete_shift(shift_id, actor)
    return success(message="Shift deleted.")


@router.post("/{shift_id}/assign", summary="Assign employees to a shift")
async def assign_shift(
    shift_id: str, payload: ShiftAssign, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    result = await shift_service.assign_shift(shift_id, payload, actor)
    return success(result, message=f"{result['assigned']} employee(s) assigned.")
