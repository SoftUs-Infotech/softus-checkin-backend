"""User lookups and administration.

The only layer that talks to the ``users`` collection.
"""

import re
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import utc_now
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import encode_for_mongo, serialize
from app.models.invite import InviteStatus
from app.models.user import UserRole, UserStatus
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


async def list_report_ids(manager_id: str) -> list[str]:
    """Everybody whose ``reporting_manager_id`` points at this account.

    References between users are stored as id strings, not ObjectIds — see
    ``PyObjectId`` in ``app/models/base.py`` — so the query matches on the
    string form the rest of the app hands around.
    """
    values = await _users().distinct(
        "_id", {"reporting_manager_id": manager_id, "is_deleted": False}
    )
    return [str(value) for value in values]


async def may_approve_for(approver: dict[str, Any], subject_user_id: str) -> bool:
    """Is this approver entitled to act on that employee's requests?"""
    if approver.get("role") == UserRole.ADMIN:
        return True
    if approver.get("role") != UserRole.MANAGER:
        return False
    return subject_user_id in await list_report_ids(approver["id"])


async def require_approval_authority(
    approver: dict[str, Any],
    subject_user_id: str,
    *,
    own_message: str = "You cannot decide your own request; an administrator does.",
    own_code: str = "CANNOT_DECIDE_OWN_REQUEST",
) -> None:
    """Whether this approver may decide a request belonging to that employee.

    Nobody signs off their own request, whatever their role — a manager's own
    leave goes to an administrator, which is the fallback for everybody. Beyond
    that an administrator decides for the whole organisation and a manager only
    for the people who report to them.

    The self-refusal is worded by the caller because each queue already had its
    own message and code, and clients match on those.
    """
    if approver["id"] == subject_user_id:
        raise PermissionDeniedError(own_message, code=own_code)
    if not await may_approve_for(approver, subject_user_id):
        raise PermissionDeniedError(
            "You can only decide requests from the people who report to you.",
            code="NOT_YOUR_REPORT",
        )


async def approval_scope(approver: dict[str, Any], user_id: str | None) -> dict[str, Any]:
    """The ``user_id`` clause that limits a queue to what this approver may see.

    An administrator sees the whole organisation, so a filter they pass is the
    only restriction. For a manager the filter can narrow the queue but never
    widen it, so asking for somebody else's requests is a refusal rather than an
    empty page — silence would read as "this person has nothing pending".
    """
    if approver.get("role") == UserRole.ADMIN:
        return {"user_id": user_id} if user_id else {}

    report_ids = await list_report_ids(approver["id"])
    if user_id is None:
        return {"user_id": {"$in": report_ids}}
    if user_id not in report_ids:
        raise PermissionDeniedError(
            "You can only see requests from the people who report to you.",
            code="NOT_YOUR_REPORT",
        )
    return {"user_id": user_id}


async def list_departments() -> list[str]:
    values = await _users().distinct("department", {"is_deleted": False})
    return sorted(value for value in values if value)


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
#: Fields an administrator may deliberately blank by sending null.
#:
#: Everything not listed keeps the old behaviour, where a null is ignored —
#: `first_name` has a minimum length and cannot be empty, and `role`,
#: `can_punch` and `wfh_enabled` are decisions rather than details, so "clear
#: it" is not a meaningful instruction for them. Clearing `shift_id` is
#: meaningful: it puts somebody back on the organisation's default shift.
CLEARABLE_FIELDS = frozenset(
    {
        "last_name",
        "phone",
        "designation",
        "department",
        "employee_code",
        "shift_id",
        "shift_from",
        "shift_to",
        "reporting_manager_id",
        "notes",
        "wfh_from",
        "wfh_to",
    }
)


async def update_user(
    user_id: str, changes: dict[str, Any], actor: Actor | None = None
) -> dict[str, Any]:
    """Apply a partial update. ``changes`` has already dropped unset fields.

    Because unset fields are already gone, a ``None`` that survives means the
    caller *sent* null — "clear this" — which is a different instruction from
    not mentioning the field. Dropping every ``None`` collapsed the two, so an
    administrator could fill a job title in but never take it out again.
    """
    existing = await require_user(user_id)

    payload = {
        key: value
        for key, value in changes.items()
        if value is not None or key in CLEARABLE_FIELDS
    }
    if not payload:
        return await require_user(user_id)

    for reference in ("shift_id", "reporting_manager_id"):
        # None is a deliberate clearing, not an identifier to validate.
        if payload.get(reference) is not None and not ObjectId.is_valid(
            str(payload[reference])
        ):
            raise ValidationError(f"'{reference}' is not a valid identifier.")

    if payload.get("reporting_manager_id") == user_id:
        raise ValidationError("An employee cannot report to themselves.")

    if payload.get("reporting_manager_id") is not None:
        await require_user(payload["reporting_manager_id"])

    # Demoting the only administrator locks everybody out of configuration,
    # salary and the audit trail, and nobody is left who can undo it. Suspension
    # and deletion have always been guarded; a role change reaches the same end.
    if "role" in payload and payload["role"] != existing.get("role"):
        await _guard_last_admin(existing)

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
    # Somebody removed before they accepted still holds a working link in their
    # inbox. Without this, following it reactivated the account and signed them
    # in — a removed administrator invite was a way back into the system.
    await get_collection(Collections.INVITES).update_many(
        {
            "user_id": {"$in": [ObjectId(user_id), user_id]},
            "status": InviteStatus.PENDING.value,
        },
        {"$set": {"status": InviteStatus.REVOKED.value, "revoked_at": utc_now()}},
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
