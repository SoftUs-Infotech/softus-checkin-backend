"""Holiday calendar.

Holidays are keyed by the same ``YYYY-MM-DD`` string the attendance collection
uses, so deciding whether a day needs a punch is a direct lookup rather than a
date comparison.
"""

from datetime import date
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import date_key, utc_now
from app.core.exceptions import ConflictError, NotFoundError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.holiday import Holiday
from app.schemas.holiday import HolidayBulkCreate, HolidayCreate, HolidayQuery, HolidayUpdate
from app.services import audit
from app.services.audit import Actor


def _holidays():
    return get_collection(Collections.HOLIDAYS)


async def require_holiday(holiday_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(holiday_id):
        raise NotFoundError("Holiday not found.", code="HOLIDAY_NOT_FOUND")
    document = await _holidays().find_one({"_id": ObjectId(holiday_id), "is_deleted": False})
    if document is None:
        raise NotFoundError("Holiday not found.", code="HOLIDAY_NOT_FOUND")
    return serialize(document)


def _build_query(query: HolidayQuery) -> dict[str, Any]:
    mongo_query: dict[str, Any] = {"is_deleted": False}
    if not query.include_inactive:
        mongo_query["is_active"] = True
    if query.year:
        mongo_query["year"] = query.year
    if query.from_date or query.to_date:
        window: dict[str, str] = {}
        if query.from_date:
            window["$gte"] = date_key(query.from_date)
        if query.to_date:
            window["$lte"] = date_key(query.to_date)
        mongo_query["date"] = window
    return mongo_query


async def list_holidays(query: HolidayQuery) -> list[dict[str, Any]]:
    cursor = _holidays().find(_build_query(query)).sort("date", 1)
    return [serialize(document) for document in await cursor.to_list(length=500)]


async def holiday_map(start: date, end: date) -> dict[str, dict[str, Any]]:
    """``{date_key: holiday}`` for a range — what the calendar and the attendance
    engine both need."""
    cursor = _holidays().find(
        {
            "is_deleted": False,
            "is_active": True,
            "date": {"$gte": date_key(start), "$lte": date_key(end)},
        }
    )
    return {
        document["date"]: serialize(document)
        for document in await cursor.to_list(length=500)
    }


async def upcoming(limit: int = 5, *, today: date | None = None) -> list[dict[str, Any]]:
    from app.core.datetime import org_today

    cursor = (
        _holidays()
        .find(
            {
                "is_deleted": False,
                "is_active": True,
                "date": {"$gte": date_key(today or org_today())},
            }
        )
        .sort("date", 1)
        .limit(limit)
    )
    return [serialize(document) for document in await cursor.to_list(length=limit)]


async def create_holiday(payload: HolidayCreate, actor: Actor) -> dict[str, Any]:
    holiday = Holiday(
        date=date_key(payload.date),
        name=payload.name,
        type=payload.type,
        description=payload.description,
        applicable_departments=payload.applicable_departments,
        year=payload.date.year,
    )
    try:
        result = await _holidays().insert_one(holiday.to_mongo())
    except DuplicateKeyError as exc:
        raise ConflictError(
            f"'{payload.name}' is already listed on {date_key(payload.date)}.",
            code="DUPLICATE_HOLIDAY",
        ) from exc

    created = await require_holiday(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.HOLIDAY,
        entity_id=created["id"],
        entity_label=f"{created['date']} {created['name']}",
        after=created,
    )
    return created


async def bulk_create(payload: HolidayBulkCreate, actor: Actor) -> dict[str, Any]:
    """Load a year at once. Duplicates are skipped and reported, not fatal."""
    years = {item.date.year for item in payload.holidays}
    if payload.replace_existing:
        await _holidays().update_many(
            {"year": {"$in": list(years)}, "is_deleted": False},
            {"$set": {"is_deleted": True, "updated_at": utc_now()}},
        )

    created: list[str] = []
    skipped: list[str] = []
    for item in payload.holidays:
        try:
            record = await create_holiday(item, actor)
            created.append(record["id"])
        except ConflictError:
            skipped.append(f"{date_key(item.date)} {item.name}")

    return {
        "created": len(created),
        "skipped": len(skipped),
        "skipped_entries": skipped,
        "years": sorted(years),
    }


async def update_holiday(holiday_id: str, payload: HolidayUpdate, actor: Actor) -> dict[str, Any]:
    existing = await require_holiday(holiday_id)
    changes = {
        key: value for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

    if "date" in changes:
        new_date: date = changes["date"]
        changes["date"] = date_key(new_date)
        changes["year"] = new_date.year

    changes["updated_at"] = utc_now()
    try:
        await _holidays().update_one({"_id": ObjectId(holiday_id)}, {"$set": changes})
    except DuplicateKeyError as exc:
        raise ConflictError(
            "Another holiday with that name already exists on that date.",
            code="DUPLICATE_HOLIDAY",
        ) from exc

    updated = await require_holiday(holiday_id)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.HOLIDAY,
        entity_id=holiday_id,
        entity_label=f"{updated['date']} {updated['name']}",
        before=existing,
        after=updated,
    )
    return updated


async def delete_holiday(holiday_id: str, actor: Actor) -> None:
    existing = await require_holiday(holiday_id)
    await _holidays().update_one(
        {"_id": ObjectId(holiday_id)},
        {"$set": {"is_deleted": True, "is_active": False, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.HOLIDAY,
        entity_id=holiday_id,
        entity_label=f"{existing['date']} {existing['name']}",
        before=existing,
    )
