"""Leave type management.

A leave type is never hard-deleted once anything references it — historical
requests and balances must keep resolving to the policy they were made under.
Deleting an unused type is a genuine soft delete; deleting a used one
deactivates it instead, and says so.
"""

from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import utc_now
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.leave_type import LeaveType
from app.schemas.leave_type import LeaveTypeCreate, LeaveTypeUpdate
from app.services import audit
from app.services.audit import Actor


def _leave_types():
    return get_collection(Collections.LEAVE_TYPES)


def _leave_requests():
    return get_collection(Collections.LEAVE_REQUESTS)


async def require_leave_type(leave_type_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(leave_type_id):
        raise NotFoundError("Leave type not found.", code="LEAVE_TYPE_NOT_FOUND")
    document = await _leave_types().find_one(
        {"_id": ObjectId(leave_type_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Leave type not found.", code="LEAVE_TYPE_NOT_FOUND")
    return serialize(document)


async def list_leave_types(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"is_deleted": False}
    if not include_inactive:
        query["is_active"] = True
    cursor = _leave_types().find(query).sort([("sort_order", 1), ("name", 1)])
    return [serialize(document) for document in await cursor.to_list(length=100)]


async def create_leave_type(payload: LeaveTypeCreate, actor: Actor) -> dict[str, Any]:
    leave_type = LeaveType(**payload.model_dump())
    try:
        result = await _leave_types().insert_one(leave_type.to_mongo())
    except DuplicateKeyError as exc:
        raise ConflictError(
            f"A leave type with the code '{payload.code}' already exists.",
            code="DUPLICATE_LEAVE_TYPE_CODE",
        ) from exc

    created = await require_leave_type(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.LEAVE_TYPE,
        entity_id=created["id"],
        entity_label=created["name"],
        after=created,
    )
    return created


async def update_leave_type(
    leave_type_id: str, payload: LeaveTypeUpdate, actor: Actor
) -> dict[str, Any]:
    existing = await require_leave_type(leave_type_id)
    changes = {
        key: value for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

    merged = {**existing, **changes}
    if merged["carry_forward_max"] > merged["annual_quota"]:
        raise ValidationError(
            "Carry-forward cannot exceed the annual quota for this leave type.",
            code="INVALID_CARRY_FORWARD",
        )

    changes["updated_at"] = utc_now()
    await _leave_types().update_one({"_id": ObjectId(leave_type_id)}, {"$set": changes})

    updated = await require_leave_type(leave_type_id)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.LEAVE_TYPE,
        entity_id=leave_type_id,
        entity_label=updated["name"],
        before=existing,
        after=updated,
    )
    return updated


async def delete_leave_type(leave_type_id: str, actor: Actor) -> dict[str, Any]:
    existing = await require_leave_type(leave_type_id)
    in_use = await _leave_requests().count_documents({"leave_type_id": leave_type_id})

    update: dict[str, Any] = {"is_active": False, "updated_at": utc_now()}
    if not in_use:
        update["is_deleted"] = True

    await _leave_types().update_one({"_id": ObjectId(leave_type_id)}, {"$set": update})
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.LEAVE_TYPE,
        entity_id=leave_type_id,
        entity_label=existing["name"],
        before=existing,
        after={"is_active": False, "is_deleted": not in_use},
    )

    if in_use:
        return {
            "deleted": False,
            "deactivated": True,
            "message": (
                f"This leave type is referenced by {in_use} request(s), so it was "
                "deactivated instead of deleted. Existing records keep their history."
            ),
        }
    return {"deleted": True, "deactivated": False, "message": "Leave type deleted."}
