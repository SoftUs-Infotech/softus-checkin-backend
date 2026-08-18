"""Employee administration endpoints. Admin-only, except where noted."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.models.user import UserRole, UserStatus
from app.schemas.common import PaginatedData
from app.schemas.user import UserAdminUpdate, UserListFilters, UserOut
from app.services import user as user_service
from app.utils.deps import CurrentAdmin, CurrentUser, Pagination
from app.utils.response import success

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", summary="List employees")
async def list_users(
    _: CurrentAdmin,
    pagination: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    role: UserRole | None = None,
    user_status: Annotated[UserStatus | None, Query(alias="status")] = None,
    department: str | None = None,
) -> dict:
    filters = UserListFilters(
        search=search, role=role, status=user_status, department=department
    )
    items, total = await user_service.list_users(filters, pagination)
    page = PaginatedData.build(
        [UserOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
    return success(page)


@router.get("/departments", summary="Distinct department names")
async def list_departments(_: CurrentUser) -> dict:
    return success(await user_service.list_departments())


@router.get("/{user_id}", summary="Get one employee")
async def get_user(user_id: str, _: CurrentAdmin) -> dict:
    return success(UserOut.from_document(await user_service.require_user(user_id)))


@router.patch("/{user_id}", summary="Update an employee")
async def update_user(user_id: str, payload: UserAdminUpdate, _: CurrentAdmin) -> dict:
    updated = await user_service.update_user(user_id, payload.model_dump(exclude_unset=True))
    return success(UserOut.from_document(updated), message="Employee updated.")


@router.post("/{user_id}/suspend", summary="Suspend an employee")
async def suspend_user(user_id: str, _: CurrentAdmin) -> dict:
    updated = await user_service.set_status(user_id, UserStatus.SUSPENDED)
    return success(UserOut.from_document(updated), message="Employee suspended.")


@router.post("/{user_id}/activate", summary="Reinstate a suspended employee")
async def activate_user(user_id: str, _: CurrentAdmin) -> dict:
    updated = await user_service.set_status(user_id, UserStatus.ACTIVE)
    return success(UserOut.from_document(updated), message="Employee reinstated.")


@router.delete("/{user_id}", summary="Remove an employee (soft delete)")
async def delete_user(user_id: str, admin: CurrentAdmin) -> dict:
    await user_service.soft_delete(user_id, acting_admin_id=admin["id"])
    return success(message="Employee removed.")
