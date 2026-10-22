"""Correction requests: raise, approve, reject, cancel.

Approval does not write a status directly. It rewrites the day's sessions and
calls :func:`attendance.recompute_day`, so an approved correction produces
exactly the same result as if the employee had punched correctly in the first
place.
"""

from datetime import date, datetime
from typing import Any

from bson import ObjectId

from app.core.datetime import attendance_date, date_key, org_today, to_utc, utc_now
from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import NotificationType
from app.models.attendance import PunchSource
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.correction import ApprovalStatus, Correction, CorrectionType
from app.schemas.bulk import BulkDecision, BulkDecisionOut, BulkResult
from app.schemas.common import PaginationParams
from app.schemas.correction import CorrectionCreate, CorrectionDecision
from app.services import attendance as attendance_service
from app.services import audit
from app.services import notification as notification_service
from app.services import settings as settings_service
from app.services import user as user_service
from app.services.audit import Actor
from app.utils import network
from app.utils.email import templates

logger = get_logger(__name__)


def _corrections():
    return get_collection(Collections.CORRECTIONS)


def _attendance():
    return get_collection(Collections.ATTENDANCE)


async def require_correction(correction_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(correction_id):
        raise NotFoundError("Correction request not found.", code="CORRECTION_NOT_FOUND")
    document = await _corrections().find_one(
        {"_id": ObjectId(correction_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Correction request not found.", code="CORRECTION_NOT_FOUND")
    return serialize(document)


async def _check_window(day: date) -> None:
    # A day before go-live is not tracked, so there is nothing to correct.
    tracking_from = await settings_service.tracking_start()
    if tracking_from is not None and day < tracking_from:
        raise ValidationError(
            f"Attendance is tracked from {tracking_from.strftime('%d %b %Y')}. "
            "Days before that are not counted, so they need no correction.",
            code="BEFORE_TRACKING_START",
        )
    org_settings = await settings_service.get_settings()
    window = int(org_settings.get("correction_window_days", 7))
    if window <= 0:
        return
    age = (org_today() - day).days
    if age > window:
        raise ValidationError(
            f"Corrections can only be requested within {window} days. "
            f"That day is {age} days old — ask an administrator to adjust it directly.",
            code="CORRECTION_WINDOW_EXPIRED",
            details={"window_days": window, "age_days": age},
        )


def _times_fall_on_the_day(payload: CorrectionCreate) -> None:
    for label, value in (
        ("punch-in", payload.requested_punch_in),
        ("punch-out", payload.requested_punch_out),
    ):
        if value is None:
            continue
        # A punch-out after midnight belongs to the previous attendance day, so
        # only the punch-in is pinned strictly to the requested date.
        if label == "punch-in" and attendance_date(to_utc(value)) != payload.date:
            raise ValidationError(
                f"The requested {label} time is not on {date_key(payload.date)}.",
                code="TIME_OUTSIDE_DAY",
            )


async def create_correction(
    user: dict[str, Any], payload: CorrectionCreate, actor: Actor
) -> dict[str, Any]:
    # Filing a correction can be pinned to the office network too, separately
    # from punching: it is the one route that can rewrite a day after the fact.
    await network.require_office_network(
        user, actor.ip, gate=network.NetworkGate.CORRECTION
    )

    if payload.date > org_today():
        raise ValidationError(
            "A correction cannot be requested for a future date.", code="FUTURE_DATE"
        )
    await _check_window(payload.date)
    _times_fall_on_the_day(payload)

    duplicate = await _corrections().find_one(
        {
            "user_id": user["id"],
            "date": date_key(payload.date),
            "status": ApprovalStatus.PENDING.value,
            "is_deleted": False,
        }
    )
    if duplicate is not None:
        raise ConflictError(
            "You already have a pending correction for that day.",
            code="CORRECTION_ALREADY_PENDING",
        )

    day_record = await _attendance().find_one(
        {"user_id": user["id"], "date": date_key(payload.date)}
    )

    correction = Correction(
        user_id=user["id"],
        user_email=user.get("email"),
        user_name=f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("email"),
        date=date_key(payload.date),
        type=payload.type,
        requested_punch_in=to_utc(payload.requested_punch_in)
        if payload.requested_punch_in
        else None,
        requested_punch_out=to_utc(payload.requested_punch_out)
        if payload.requested_punch_out
        else None,
        reason=payload.reason,
        previous_status=(day_record or {}).get("status"),
        previous_worked_minutes=(day_record or {}).get("worked_minutes"),
    )
    result = await _corrections().insert_one(correction.to_mongo())

    created = await require_correction(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.CORRECTION,
        entity_id=created["id"],
        entity_label=f"{user['email']} {created['date']}",
        after={"type": created["type"], "date": created["date"], "reason": created["reason"]},
    )
    await notification_service.notify_approvers(
        user["id"],
        type=NotificationType.APPROVAL_PENDING,
        title="Attendance correction awaiting approval",
        body=f"{created['user_name']} requested a correction for {created['date']}.",
        link="/admin/corrections",
    )
    logger.info("Correction requested by %s for %s", user["email"], created["date"])
    return created


async def list_corrections(
    pagination: PaginationParams,
    *,
    user_id: str | None = None,
    status: str | None = None,
    approver: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    query: dict[str, Any] = {"is_deleted": False}
    if approver is not None:
        query.update(await user_service.approval_scope(approver, user_id))
    elif user_id:
        query["user_id"] = user_id
    if status:
        query["status"] = status

    total = await _corrections().count_documents(query)
    cursor = (
        _corrections()
        .find(query)
        .sort("created_at", -1)
        .skip(pagination.skip)
        .limit(pagination.limit)
    )
    items = [serialize(document) for document in await cursor.to_list(length=pagination.limit)]
    return items, total


async def pending_count(approver: dict[str, Any] | None = None) -> int:
    # No approver means the whole organisation — the Monday digest counts for
    # administrators, and it has nobody's session to scope by.
    query: dict[str, Any] = {"status": ApprovalStatus.PENDING.value, "is_deleted": False}
    if approver is not None:
        query.update(await user_service.approval_scope(approver, None))
    return await _corrections().count_documents(query)


def _build_sessions(
    correction: dict[str, Any], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn an approved request into the day's sessions.

    A missed punch-out fills in the open session rather than adding a new one;
    anything else replaces the day, which is what the employee asked for.
    """
    note = f"Correction approved: {correction['reason'][:200]}"

    def event(at: datetime) -> dict[str, Any]:
        return {
            "at": to_utc(at),
            "source": PunchSource.CORRECTION.value,
            "note": note,
        }

    correction_type = correction["type"]
    punch_in = correction.get("requested_punch_in")
    punch_out = correction.get("requested_punch_out")

    if correction_type == CorrectionType.MISSED_PUNCH_OUT.value:
        sessions = [dict(session) for session in existing]
        for session in sessions:
            if session.get("punch_out") is None:
                session["punch_out"] = event(punch_out)
                session["auto_closed"] = False
                return sessions
        # Nothing open to close — fall through and record the pair.
        if punch_in:
            sessions.append({"punch_in": event(punch_in), "punch_out": event(punch_out),
                             "auto_closed": False})
            return sessions
        raise ValidationError(
            "There is no open session on that day to close.", code="NO_OPEN_SESSION"
        )

    if correction_type == CorrectionType.MISSED_PUNCH_IN.value and not punch_out:
        sessions = [dict(session) for session in existing]
        sessions.insert(0, {"punch_in": event(punch_in), "punch_out": None,
                            "auto_closed": False})
        return sessions

    return [
        {
            "punch_in": event(punch_in),
            "punch_out": event(punch_out) if punch_out else None,
            "auto_closed": False,
        }
    ]


async def decide(
    correction_id: str,
    approve: bool,
    payload: CorrectionDecision,
    approver: dict[str, Any],
    actor: Actor,
) -> dict[str, Any]:
    correction = await require_correction(correction_id)
    await user_service.require_approval_authority(approver, correction["user_id"])
    if correction["status"] != ApprovalStatus.PENDING.value:
        raise ConflictError(
            f"This request was already {correction['status'].lower()}.",
            code="CORRECTION_ALREADY_DECIDED",
        )

    day = date.fromisoformat(correction["date"])
    now = utc_now()

    if approve:
        user = serialize(
            await get_collection(Collections.USERS).find_one(
                {"_id": ObjectId(correction["user_id"])}
            )
        )
        if user is None:
            raise NotFoundError("This employee no longer exists.", code="USER_NOT_FOUND")

        existing_document = await _attendance().find_one(
            {"user_id": correction["user_id"], "date": correction["date"]}
        )
        sessions = _build_sessions(correction, (existing_document or {}).get("sessions") or [])

        shift = await attendance_service.shift_for_user(user)
        document = await attendance_service.ensure_day_document(user, day, shift)
        await _attendance().update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "sessions": sessions,
                    "is_manually_adjusted": True,
                    "adjusted_by": approver["id"],
                    "adjusted_at": now,
                    "adjustment_note": f"Correction #{correction_id} approved.",
                    "updated_at": now,
                }
            },
        )
        await attendance_service.recompute_day(correction["user_id"], day, user=user)

    await _corrections().update_one(
        {"_id": ObjectId(correction_id)},
        {
            "$set": {
                "status": (
                    ApprovalStatus.APPROVED.value if approve else ApprovalStatus.REJECTED.value
                ),
                "approver_id": approver["id"],
                "approver_email": approver.get("email"),
                "approver_role": approver["role"],
                "decided_at": now,
                "decision_note": payload.note,
                "updated_at": now,
            }
        },
    )

    updated = await require_correction(correction_id)
    await audit.record(
        actor,
        AuditAction.APPROVE if approve else AuditAction.REJECT,
        AuditEntity.CORRECTION,
        entity_id=correction_id,
        entity_label=f"{correction.get('user_email')} {correction['date']}",
        before={"status": ApprovalStatus.PENDING.value},
        after={"status": updated["status"], "note": payload.note},
    )
    await notification_service.notify(
        correction["user_id"],
        type=NotificationType.CORRECTION_DECIDED,
        title=f"Your correction for {correction['date']} was "
        f"{'approved' if approve else 'rejected'}",
        body=payload.note,
        link="/corrections",
        # A regularisation is usually filed because the day was already wrong,
        # and the person is waiting to know whether it has been put right. The
        # bell only tells them next time they open the app.
        email=(
            templates.correction_decision_email(
                to=correction["user_email"],
                name=(correction.get("user_name") or "").split(" ")[0],
                day=correction["date"],
                approved=approve,
                note=payload.note,
            )
            if correction.get("user_email")
            else None
        ),
    )
    logger.info(
        "Correction %s %s by %s",
        correction_id,
        "approved" if approve else "rejected",
        approver.get("email"),
    )
    return updated


async def cancel(correction_id: str, user: dict[str, Any], actor: Actor) -> dict[str, Any]:
    correction = await require_correction(correction_id)
    if correction["user_id"] != user["id"]:
        raise NotFoundError("Correction request not found.", code="CORRECTION_NOT_FOUND")
    if correction["status"] != ApprovalStatus.PENDING.value:
        raise ConflictError(
            "Only a pending request can be withdrawn.", code="CORRECTION_ALREADY_DECIDED"
        )

    await _corrections().update_one(
        {"_id": ObjectId(correction_id)},
        {"$set": {"status": ApprovalStatus.CANCELLED.value, "updated_at": utc_now()}},
    )
    updated = await require_correction(correction_id)
    await audit.record(
        actor,
        AuditAction.UPDATE,
        AuditEntity.CORRECTION,
        entity_id=correction_id,
        entity_label=f"{user['email']} {correction['date']}",
        after={"status": ApprovalStatus.CANCELLED.value},
    )
    return updated


async def bulk_decide(
    payload: BulkDecision, approver: dict[str, Any], actor: Actor
) -> BulkDecisionOut:
    """One decision and one note across many requests.

    A bad id is reported against that id alone: clearing a morning's queue must
    not lose nineteen good decisions because the twentieth was withdrawn in
    another tab.
    """
    decision = CorrectionDecision(note=payload.note)
    results: list[BulkResult] = []

    for record_id in dict.fromkeys(payload.ids):
        try:
            await decide(record_id, payload.approve, decision, approver, actor)
        except AppError as error:
            results.append(
                BulkResult(
                    id=record_id,
                    success=False,
                    error_code=error.code,
                    message=error.message,
                )
            )
        else:
            results.append(BulkResult(id=record_id, success=True))

    succeeded = sum(1 for result in results if result.success)
    return BulkDecisionOut(
        approved=payload.approve,
        requested=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )
