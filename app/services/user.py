"""User lookups and administration.

The only layer that talks to the ``users`` collection.
"""

import re
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import utc_now
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import encode_for_mongo, serialize
from app.models.user import UserStatus
from app.schemas.common import PaginationParams
from app.schemas.user import UserListFilters
from app.services import audit
from app.services.audit import Actor


def _users():
    return get_collection(Collections.USERS)


def _refresh_tokens():
    return get_collection(Collections.REFRESH_TOKENS)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(user_id):
        return None
    return serialize(await _users().find_one({"_id": ObjectId(user_id), "is_deleted": False}))


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    return serialize(await _users().find_one({"email": email.strip().lower(), "is_deleted": False}))


async def require_user(user_id: str) -> dict[str, Any]:
    user = await get_user_by_id(user_id)
    if user is None:
        raise NotFoundError("This employee was not found.", code="USER_NOT_FOUND")
    return user


async def list_users(
    filters: UserListFilters, pagination: PaginationParams
) -> tuple[list[dict[str, Any]], int]:
    query: dict[str, Any] = {"is_deleted": False}

    if filters.role:
        query["role"] = filters.role.value
    if filters.status:
        query["status"] = filters.status.value
    if filters.department:
        query["department"] = filters.department
    if filters.search:
        pattern = re.escape(filters.search.strip())
        query["$or"] = [
            {"first_name": {"$regex": pattern, "$options": "i"}},
            {"last_name": {"$regex": pattern, "$options": "i"}},
            {"email": {"$regex": pattern, "$options": "i"}},
            {"employee_code": {"$regex": pattern, "$options": "i"}},
        ]

    total = await _users().count_documents(query)
    cursor = (
        _users()
        .find(query)
        .sort([("first_name", 1), ("email", 1)])
        .skip(pagination.skip)
        .limit(pagination.limit)
    )
    items = [serialize(document) for document in await cursor.to_list(length=pagination.limit)]
    return items, total


async def list_departments() -> list[str]:
    values = await _users().distinct("department", {"is_deleted": False})
    return sorted(value for value in values if value)


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
async def update_user(
    user_id: str, changes: dict[str, Any], actor: Actor | None = None
) -> dict[str, Any]:
    """Apply a partial update. ``changes`` has already dropped unset fields."""
    existing = await require_user(user_id)

    payload = {key: value for key, value in changes.items() if value is not None}
    if not payload:
        return await require_user(user_id)

    for reference in ("shift_id", "reporting_manager_id"):
        if reference in payload and not ObjectId.is_valid(str(payload[reference])):
            raise ValidationError(f"'{reference}' is not a valid identifier.")

    if payload.get("reporting_manager_id") == user_id:
        raise ValidationError("An employee cannot report to themselves.")

    if "reporting_manager_id" in payload:
        await require_user(payload["reporting_manager_id"])

    payload["updated_at"] = utc_now()
    try:
        await _users().update_one(
            {"_id": ObjectId(user_id)}, {"$set": encode_for_mongo(payload)}
        )
    except DuplicateKeyError as exc:
        raise ConflictError(
            "That employee code is already assigned to someone else.",
            code="DUPLICATE_EMPLOYEE_CODE",
        ) from exc

    updated = await require_user(user_id)
    if actor is not None:
        await audit.record_change(
            actor,
            AuditAction.UPDATE,
            AuditEntity.USER,
            entity_id=user_id,
            entity_label=updated.get("email"),
            before=existing,
            after=updated,
        )
    return updated


async def set_status(
    user_id: str, status: UserStatus, actor: Actor | None = None
) -> dict[str, Any]:
    user = await require_user(user_id)
    if user["status"] == status.value:
        return user

    if status is not UserStatus.ACTIVE:
        await _guard_last_admin(user)

    await _users().update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"status": status.value, "updated_at": utc_now()}},
    )
    if status is UserStatus.SUSPENDED:
        # A suspended employee should lose access immediately, not at token expiry.
        await _refresh_tokens().update_many(
            {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": utc_now()}}
        )

    updated = await require_user(user_id)
    if actor is not None:
        await audit.record(
            actor,
            AuditAction.SUSPEND if status is UserStatus.SUSPENDED else AuditAction.ACTIVATE,
            AuditEntity.USER,
            entity_id=user_id,
            entity_label=updated.get("email"),
            before={"status": user["status"]},
            after={"status": status.value},
        )
    return updated


async def soft_delete(
    user_id: str, *, acting_admin_id: str, actor: Actor | None = None
) -> None:
    if user_id == acting_admin_id:
        raise ValidationError("You cannot remove your own account.", code="CANNOT_DELETE_SELF")

    user = await require_user(user_id)
    await _guard_last_admin(user)
    await _users().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "is_deleted": True,
                "status": UserStatus.SUSPENDED.value,
                "updated_at": utc_now(),
            }
        },
    )
    await _refresh_tokens().update_many(
        {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": utc_now()}}
    )
    if actor is not None:
        await audit.record(
            actor,
            AuditAction.DELETE,
            AuditEntity.USER,
            entity_id=user_id,
            entity_label=user.get("email"),
            before=user,
        )


async def _guard_last_admin(user: dict[str, Any]) -> None:
    """Refuse to remove the only way back into the system."""
    if user.get("role") != "ADMIN" or user.get("status") != UserStatus.ACTIVE.value:
        return
    if await count_admins() <= 1:
        raise ValidationError(
            "This is the only active administrator. Promote another admin first.",
            code="LAST_ADMIN",
        )


async def count_admins() -> int:
    return await _users().count_documents(
        {"role": "ADMIN", "is_deleted": False, "status": UserStatus.ACTIVE.value}
    )
