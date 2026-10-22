"""Shift management.

Two invariants this module owns:

* Exactly one shift is the default. Promoting one demotes the rest, in that
  order, so a crash between the two writes leaves zero defaults rather than two —
  and zero is recoverable by promoting again, whereas two is ambiguous.
* A shift that employees are assigned to cannot be deleted out from under them.
"""

from datetime import date, datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import date_key, org_today, utc_now
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.shift import Shift
from app.schemas.shift import ShiftAssign, ShiftCreate, ShiftUpdate
from app.services import audit
from app.services.audit import Actor


def _shifts():
    return get_collection(Collections.SHIFTS)


def _users():
    return get_collection(Collections.USERS)


async def _require_object_id(shift_id: str) -> ObjectId:
    if not ObjectId.is_valid(shift_id):
        raise NotFoundError("Shift not found.", code="SHIFT_NOT_FOUND")
    return ObjectId(shift_id)


async def require_shift(shift_id: str) -> dict[str, Any]:
    document = await _shifts().find_one(
        {"_id": await _require_object_id(shift_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Shift not found.", code="SHIFT_NOT_FOUND")
    return serialize(document)


async def list_shifts(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"is_deleted": False}
    if not include_inactive:
        query["is_active"] = True
    cursor = _shifts().find(query).sort([("is_default", -1), ("name", 1)])
    return [serialize(document) for document in await cursor.to_list(length=200)]


async def get_default_shift() -> dict[str, Any] | None:
    document = await _shifts().find_one(
        {"is_default": True, "is_active": True, "is_deleted": False}
    )
    return serialize(document)


async def count_assigned(shift_id: str, *, on: date | None = None) -> int:
    """How many people this shift is still spoken for by.

    Now that an assignment carries a window, "is anybody on this shift" and "does
    anybody's row still point at it" are different questions. A window that ended
    in March does not put somebody on the shift today, and counting it both
    overstated the figure on the card and refused a deletion nobody was relying
    on any more.

    A window that has not started yet *does* count: somebody is scheduled onto
    this shift next month, and deleting it out from under that plan is exactly
    what this guard exists to prevent.
    """
    today = date_key(on or org_today())
    return await _users().count_documents(
        {
            "shift_id": shift_id,
            "is_deleted": False,
            # Open-ended, or ending today or later.
            "$or": [
                {"shift_to": None},
                {"shift_to": {"$exists": False}},
                {"shift_to": {"$gte": today}},
            ],
        }
    )


async def _clear_other_defaults(keep_id: ObjectId) -> None:
    await _shifts().update_many(
        {"_id": {"$ne": keep_id}, "is_default": True},
        {"$set": {"is_default": False, "updated_at": utc_now()}},
    )


async def create_shift(payload: ShiftCreate, actor: Actor) -> dict[str, Any]:
    shift = Shift(**payload.model_dump())

    # The first shift created is the default whether or not it was asked for —
    # otherwise employees can be created with nothing to measure them against.
    if await _shifts().count_documents({"is_deleted": False}) == 0:
        shift.is_default = True

    try:
        result = await _shifts().insert_one(shift.to_mongo())
    except DuplicateKeyError as exc:
        raise ConflictError(
            f"A shift with the code '{payload.code}' already exists.",
            code="DUPLICATE_SHIFT_CODE",
        ) from exc

    if shift.is_default:
        await _clear_other_defaults(result.inserted_id)

    created = await require_shift(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.SHIFT,
        entity_id=created["id"],
        entity_label=created["name"],
        after=created,
    )
    return created


async def update_shift(shift_id: str, payload: ShiftUpdate, actor: Actor) -> dict[str, Any]:
    existing = await require_shift(shift_id)
    changes = {
        key: value for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

    merged = {**existing, **changes}
    if merged["half_day_minutes"] >= merged["required_minutes"]:
        raise ValidationError(
            "Half-day hours must be less than the hours required for a full day.",
            code="INVALID_SHIFT_HOURS",
        )
    if changes.get("is_active") is False and existing.get("is_default"):
        raise ValidationError(
            "The default shift cannot be deactivated. Make another shift the default first.",
            code="CANNOT_DEACTIVATE_DEFAULT",
        )
    if changes.get("is_default") is False and existing.get("is_default"):
        raise ValidationError(
            "Promote another shift to default instead of clearing this one.",
            code="DEFAULT_REQUIRED",
        )

    object_id = ObjectId(shift_id)
    changes["updated_at"] = utc_now()
    await _shifts().update_one({"_id": object_id}, {"$set": changes})
    if changes.get("is_default"):
        await _clear_other_defaults(object_id)

    updated = await require_shift(shift_id)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.SHIFT,
        entity_id=shift_id,
        entity_label=updated["name"],
        before=existing,
        after=updated,
    )
    return updated


async def delete_shift(shift_id: str, actor: Actor) -> None:
    existing = await require_shift(shift_id)

    if existing.get("is_default"):
        raise ValidationError(
            "The default shift cannot be deleted. Make another shift the default first.",
            code="CANNOT_DELETE_DEFAULT",
        )

    assigned = await count_assigned(shift_id)
    if assigned:
        raise ConflictError(
            f"{assigned} employee(s) are on this shift. Move them to another shift first.",
            code="SHIFT_IN_USE",
            details={"assigned_count": assigned},
        )

    await _shifts().update_one(
        {"_id": ObjectId(shift_id)},
        {"$set": {"is_deleted": True, "is_active": False, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.SHIFT,
        entity_id=shift_id,
        entity_label=existing["name"],
        before=existing,
    )


async def assign_shift(shift_id: str, payload: ShiftAssign, actor: Actor) -> dict[str, Any]:
    shift = await require_shift(shift_id)
    if not shift.get("is_active"):
        raise ValidationError(
            "Employees cannot be assigned to an inactive shift.", code="SHIFT_INACTIVE"
        )

    invalid = [uid for uid in payload.user_ids if not ObjectId.is_valid(uid)]
    if invalid:
        raise ValidationError(
            "One or more employee identifiers are not valid.",
            code="INVALID_USER_IDS",
            details={"invalid": invalid},
        )

    object_ids = [ObjectId(uid) for uid in payload.user_ids]
    found = await _users().count_documents({"_id": {"$in": object_ids}, "is_deleted": False})
    if found != len(object_ids):
        raise NotFoundError(
            "One or more of those employees no longer exist.", code="USER_NOT_FOUND"
        )

    result = await _users().update_many(
        {"_id": {"$in": object_ids}, "is_deleted": False},
        {"$set": {"shift_id": shift_id, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.ASSIGN,
        AuditEntity.SHIFT,
        entity_id=shift_id,
        entity_label=shift["name"],
        after={"assigned_user_ids": payload.user_ids},
    )
    return {"shift_id": shift_id, "assigned": result.modified_count}


def assignment_active(user: dict[str, Any], on: date) -> bool:
    """Is this employee's shift assignment live on ``on``?

    A shift is assigned for a stretch of work — "the general shift for these two
    weeks" — so it has a window. Absent dates mean open-ended, which is both how
    a standing assignment is expressed and how every account written before the
    window existed behaves.
    """
    if not user.get("shift_id"):
        return False

    day = date_key(on)
    starts = _as_date_key(user.get("shift_from"))
    ends = _as_date_key(user.get("shift_to"))

    started = not starts or starts <= day
    not_finished = not ends or day <= ends
    return started and not_finished


def _as_date_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10] or None
    if isinstance(value, date | datetime):
        return date_key(value)
    return None


def assignment_lapsed(user: dict[str, Any], on: date) -> bool:
    """Was this employee planned onto a shift whose window has since run out?

    Deliberately narrower than "has no active assignment". Somebody who was
    never assigned a shift is not a lapse — they are an organisation that does
    not plan work this way, and flagging every one of their days would fill the
    exception feed with a fact the administrator already knows.

    A *lapse* is the thing worth chasing: a window was planned, it has ended,
    and the person is still turning up — which is exactly the case this feature
    was asked for. "My expectation was to finish within these five days, and it
    is already over."
    """
    if not user.get("shift_id"):
        return False
    ends = _as_date_key(user.get("shift_to"))
    return bool(ends) and ends < date_key(on)


async def resolve_for_user(
    user: dict[str, Any], *, on: date | None = None
) -> dict[str, Any] | None:
    """The shift an employee is measured against — theirs, else the default.

    The default shift is what *timings* fall back to, and it always exists, so
    the day can still be judged. Whether the employee has an assignment of their
    own is a separate question, asked through ``assignment_active`` — a missing
    assignment must never stop somebody punching, it is a planning signal for
    the administrator.
    """
    shift_id = user.get("shift_id")
    if shift_id and ObjectId.is_valid(shift_id) and assignment_active(user, on or org_today()):
        document = await _shifts().find_one({"_id": ObjectId(shift_id), "is_deleted": False})
        if document is not None:
            return serialize(document)
    return await get_default_shift()
