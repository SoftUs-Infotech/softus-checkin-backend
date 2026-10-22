"""Comp-off: credit, balance, and expiry that nobody has to remember to run.

A credit is one row per employee per earned day — a ledger. Anybody who actually
worked the day gets one; the rest of the organisation does not, which is why the
credit is driven off attendance documents rather than the headcount.

Expiry is derived on read. ``effective_status`` compares ``expires_on`` against
today, so a credit lapses on its own date whether or not a scheduled job ever
runs. Nothing is rewritten in the database when a credit dies: the row keeps
saying what was earned and when, and the reader draws the conclusion.
"""

from datetime import date, timedelta
from typing import Any

from bson import ObjectId

from app.core.datetime import date_key, org_today, utc_now
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.comp_off import CompOff, CompOffStatus
from app.models.user import UserRole
from app.services import audit
from app.services import settings as settings_service
from app.services.audit import Actor


def _comp_offs():
    return get_collection(Collections.COMP_OFFS)


def _attendance():
    return get_collection(Collections.ATTENDANCE)


# --------------------------------------------------------------------------- #
# pure rules
# --------------------------------------------------------------------------- #
def expiry_date(earned_on: date, expiry_days: int) -> str | None:
    """``None`` when the organisation has set the expiry to zero — never lapses."""
    if expiry_days <= 0:
        return None
    return date_key(earned_on + timedelta(days=expiry_days))


def effective_status(document: dict[str, Any], *, today: date) -> str:
    """What the row means now. A spent credit stays spent; an old one lapses."""
    if document.get("status") == CompOffStatus.USED:
        return CompOffStatus.USED
    expires_on = document.get("expires_on")
    if expires_on and expires_on < date_key(today):
        return CompOffStatus.EXPIRED
    return CompOffStatus.AVAILABLE


def with_effective_status(
    documents: list[dict[str, Any]], *, today: date | None = None
) -> list[dict[str, Any]]:
    today = today or org_today()
    return [
        {**document, "status": effective_status(document, today=today)}
        for document in documents
    ]


def summarise(documents: list[dict[str, Any]], *, today: date | None = None) -> dict[str, int]:
    resolved = with_effective_status(documents, today=today)
    return {
        "available": sum(1 for item in resolved if item["status"] == CompOffStatus.AVAILABLE),
        "used": sum(1 for item in resolved if item["status"] == CompOffStatus.USED),
        "expired": sum(1 for item in resolved if item["status"] == CompOffStatus.EXPIRED),
        "total": len(resolved),
    }


# --------------------------------------------------------------------------- #
# crediting
# --------------------------------------------------------------------------- #
async def _worked_on(day: date) -> list[dict[str, Any]]:
    """Everybody with recorded time on a date — not everybody who was rostered."""
    cursor = _attendance().find(
        {"date": date_key(day), "is_deleted": False, "worked_minutes": {"$gt": 0}}
    )
    return [serialize(document) for document in await cursor.to_list(length=1000)]


async def _emails_for(user_ids: list[str]) -> dict[str, str]:
    """One lookup for the whole batch — the ledger row carries the address so a
    credit still reads sensibly after an account is renamed or removed."""
    cursor = get_collection(Collections.USERS).find(
        {"_id": {"$in": [ObjectId(value) for value in user_ids if ObjectId.is_valid(value)]}},
        {"email": 1},
    )
    return {
        str(document["_id"]): document.get("email")
        for document in await cursor.to_list(length=1000)
    }


async def credit_for_date(day: date, *, reason: str, actor: Actor) -> int:
    """Credit every employee who worked ``day``. Returns how many were credited.

    Idempotent: re-ruling the same date twice must not hand out a second credit,
    so an existing row for the same employee and day is left alone.

    Each credit is audited on its own rather than as a batch: a comp-off is a
    benefit granted to one named person, and "twelve credits issued" is not an
    answer to "why does she have this day".
    """
    org = await settings_service.get_settings()
    expiry_days = int(org.get("comp_off_expiry_days", 0) or 0)
    expires_on = expiry_date(day, expiry_days)

    worked = await _worked_on(day)
    emails = await _emails_for([item["user_id"] for item in worked if item.get("user_id")])

    credited = 0
    for attendance in worked:
        user_id = attendance.get("user_id")
        if not user_id:
            continue
        existing = await _comp_offs().find_one(
            {"user_id": user_id, "earned_on": date_key(day), "is_deleted": False}
        )
        if existing is not None:
            continue

        record = CompOff(
            user_id=user_id,
            user_email=emails.get(user_id),
            earned_on=date_key(day),
            reason=reason,
            expires_on=expires_on,
        )
        result = await _comp_offs().insert_one(record.to_mongo())
        credited += 1

        created = serialize(await _comp_offs().find_one({"_id": result.inserted_id}))
        await audit.record(
            actor,
            AuditAction.CREATE,
            AuditEntity.COMP_OFF,
            entity_id=created["id"],
            entity_label=f"{date_key(day)} to {emails.get(user_id) or user_id}",
            after=created,
        )

    return credited


# --------------------------------------------------------------------------- #
# reading and spending
# --------------------------------------------------------------------------- #
def target_user_id(current_user: dict[str, Any], requested: str | None) -> str:
    """Whose ledger is being asked for.

    An employee may read only their own; an administrator may name anybody. The
    check lives here rather than in the route so the rule has one home.
    """
    own = current_user.get("id")
    if not requested or requested == own:
        return own
    if current_user.get("role") != UserRole.ADMIN:
        raise PermissionDeniedError("You can only see your own comp-off ledger.")
    return requested



async def list_for_user(
    user_id: str, *, include_spent: bool = True, today: date | None = None
) -> list[dict[str, Any]]:
    cursor = (
        _comp_offs()
        .find({"user_id": user_id, "is_deleted": False})
        .sort("earned_on", -1)
    )
    documents = with_effective_status(
        [serialize(item) for item in await cursor.to_list(length=500)], today=today
    )
    if include_spent:
        return documents
    return [item for item in documents if item["status"] == CompOffStatus.AVAILABLE]


def require_org_ledger_access(current_user: dict[str, Any]) -> None:
    """Only an administrator reads other people's credits."""
    if current_user.get("role") != UserRole.ADMIN:
        raise PermissionDeniedError("You can only see your own comp-off ledger.")


async def list_for_org(
    *, include_spent: bool = True, today: date | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    """The whole organisation's ledger, newest first.

    An administrator who closes a day wants to see who it cost, and they will
    never find it in their own ledger — a comp-off is earned by working, and the
    person doing the closing is generally not the person who worked.
    """
    cursor = _comp_offs().find({"is_deleted": False}).sort("earned_on", -1)
    documents = with_effective_status(
        [serialize(item) for item in await cursor.to_list(length=limit)], today=today
    )
    if not include_spent:
        documents = [item for item in documents if item["status"] == CompOffStatus.AVAILABLE]
    await _attach_names(documents)
    return documents


async def _attach_names(documents: list[dict[str, Any]]) -> None:
    """Name the person on each row — a ledger of email addresses reads like a log.

    Resolved at read time, so a rename shows; the stored email stays the fallback
    for an account that has since been removed.
    """
    ids = {item["user_id"] for item in documents if ObjectId.is_valid(item.get("user_id", ""))}
    if not ids:
        return
    users = await get_collection(Collections.USERS).find(
        {"_id": {"$in": [ObjectId(user_id) for user_id in ids]}},
        {"first_name": 1, "last_name": 1},
    ).to_list(length=len(ids))
    names = {
        str(user["_id"]): f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        for user in users
    }
    for item in documents:
        item["user_name"] = names.get(item["user_id"]) or None


async def balance(user_id: str, *, today: date | None = None) -> dict[str, Any]:
    documents = await list_for_user(user_id, today=today)
    return {"user_id": user_id, **summarise(documents, today=today)}


async def spend(
    comp_off_id: str,
    *,
    used_on: date,
    reference: str | None = None,
    actor: Actor | None = None,
) -> dict[str, Any]:
    """Mark a credit spent. Refuses an expired one — it is not there to spend."""
    if not ObjectId.is_valid(comp_off_id):
        raise NotFoundError("Comp-off not found.", code="COMP_OFF_NOT_FOUND")
    document = await _comp_offs().find_one(
        {"_id": ObjectId(comp_off_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Comp-off not found.", code="COMP_OFF_NOT_FOUND")

    status = effective_status(serialize(document), today=used_on)
    if status != CompOffStatus.AVAILABLE:
        raise ConflictError(
            f"That comp-off is {status.lower()} and cannot be used.",
            code="COMP_OFF_NOT_AVAILABLE",
        )

    await _comp_offs().update_one(
        {"_id": ObjectId(comp_off_id)},
        {
            "$set": {
                "status": CompOffStatus.USED,
                "used_on": date_key(used_on),
                "used_at": utc_now(),
                "used_reference": reference,
                "updated_at": utc_now(),
            }
        },
    )
    spent = serialize(await _comp_offs().find_one({"_id": ObjectId(comp_off_id)}))
    await audit.record_change(
        actor or audit.SYSTEM,
        AuditAction.UPDATE,
        AuditEntity.COMP_OFF,
        entity_id=comp_off_id,
        entity_label=f"Earned {spent['earned_on']}",
        before=serialize(document),
        after=spent,
    )
    return spent
