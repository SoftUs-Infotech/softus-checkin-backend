"""Attendance: punching, day recomputation, calendars and admin adjustment.

Every write funnels through :func:`recompute_day`, which re-derives status and
worked minutes from the stored sessions using :mod:`attendance_rules`. Nothing
else is allowed to set a status — that is how a punch, an approved correction
and an admin adjustment all end up agreeing.
"""

import re
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import (
    attendance_date,
    date_key,
    iter_dates,
    month_range,
    org_today,
    to_utc,
    utc_now,
)
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import NotificationType
from app.models.attendance import (
    STATUS_COLORS,
    STATUS_LABELS,
    Attendance,
    AttendanceStatus,
    PunchRequest,
    PunchSource,
)
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.user import may_punch as user_may_punch
from app.schemas.attendance import (
    AttendanceAdjust,
    AttendanceDayOut,
    AttendanceQuery,
    MonthCalendarOut,
    MonthSummary,
    PunchRequestBody,
    RegisterMeta,
    RegisterQuery,
    RegisterSort,
    SortDirection,
)
from app.services import audit
from app.services import geocode as geocode_service
from app.services import holiday as holiday_service
from app.services import notification as notification_service
from app.services import settings as settings_service
from app.services import shift as shift_service
from app.services import work_calendar as work_calendar_service
from app.services.attendance_rules import compute_day_status, employment_start, format_minutes
from app.services.audit import Actor
from app.utils import network

logger = get_logger(__name__)


def _attendance():
    return get_collection(Collections.ATTENDANCE)


def _punch_requests():
    return get_collection(Collections.PUNCH_REQUESTS)


def _users():
    return get_collection(Collections.USERS)


def _leave_requests():
    return get_collection(Collections.LEAVE_REQUESTS)


# --------------------------------------------------------------------------- #
# supporting lookups
# --------------------------------------------------------------------------- #
async def approved_leave_map(
    user_id: str, start: date, end: date
) -> dict[str, dict[str, Any]]:
    """``{date_key: leave}`` for approved leave in a range.

    Reads the leave collection directly rather than importing the leave service,
    which does not exist until a later phase. The shape is fixed by the model.
    """
    cursor = _leave_requests().find(
        {
            "user_id": user_id,
            "status": "APPROVED",
            "is_deleted": False,
            "days.date": {"$gte": date_key(start), "$lte": date_key(end)},
        }
    )
    result: dict[str, dict[str, Any]] = {}
    for request in await cursor.to_list(length=500):
        for day in request.get("days", []):
            day_key = day.get("date")
            if day_key and date_key(start) <= day_key <= date_key(end):
                result[day_key] = {
                    "day_part": day.get("day_part", "FULL"),
                    "leave_type_id": request.get("leave_type_id"),
                    "leave_type_name": request.get("leave_type_name"),
                    "request_id": str(request.get("_id")),
                }
    return result


async def shift_for_user(user: dict[str, Any]) -> dict[str, Any] | None:
    return await shift_service.resolve_for_user(user)


def _session_dicts(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document.get("sessions") or []


# --------------------------------------------------------------------------- #
# the one place a day's status is decided
# --------------------------------------------------------------------------- #
async def recompute_day(
    user_id: str,
    day: date,
    *,
    user: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Re-derive a day from its stored sessions and save the result."""
    if user is None:
        user = serialize(await _users().find_one({"_id": ObjectId(user_id)}))
    if user is None:
        raise NotFoundError("This employee was not found.", code="USER_NOT_FOUND")

    day_key = date_key(day)
    document = await _attendance().find_one({"user_id": user_id, "date": day_key})
    shift = (document or {}).get("shift_snapshot") or await shift_for_user(user)

    holidays = await holiday_service.holiday_map(day, day)
    leaves = await approved_leave_map(user_id, day, day)

    computed = compute_day_status(
        day,
        _session_dicts(document or {}),
        shift,
        today=org_today(),
        now=utc_now(),
        holiday=holidays.get(day_key),
        leave=leaves.get(day_key),
        # A swapped Saturday, or a day an administrator opened by hand, is a
        # working day however the shift's weekday pattern reads.
        working_day=await work_calendar_service.is_working_day(day, user=user),
        employed_from=employment_start(user),
    )

    update = {
        "worked_minutes": computed.worked_minutes,
        "status": computed.status.value,
        "is_late": computed.is_late,
        "first_punch_in": computed.first_punch_in,
        "last_punch_out": computed.last_punch_out,
        "updated_at": utc_now(),
    }

    if persist and document is not None:
        await _attendance().update_one({"_id": document["_id"]}, {"$set": update})
        document = await _attendance().find_one({"_id": document["_id"]})

    if document is None:
        # Nothing stored for this day — describe it without creating a row.
        document = {
            "user_id": user_id,
            "date": day_key,
            "sessions": [],
            "shift_snapshot": shift,
            **update,
        }

    enriched = serialize(document)
    enriched["holiday"] = holidays.get(day_key)
    enriched["leave"] = leaves.get(day_key)
    enriched["expected_minutes"] = computed.expected_minutes
    enriched["shortfall_minutes"] = computed.shortfall_minutes
    enriched["has_open_session"] = computed.has_open_session
    return enriched


def to_day_out(record: dict[str, Any]) -> AttendanceDayOut:
    """Present a computed day for the API."""
    sessions = []
    for session in record.get("sessions") or []:
        punch_in = session.get("punch_in") or {}
        punch_out = session.get("punch_out")
        duration = 0
        if punch_in.get("at") and punch_out and punch_out.get("at"):
            duration = int(
                (to_utc(punch_out["at"]) - to_utc(punch_in["at"])).total_seconds() // 60
            )
        sessions.append(
            {
                "punch_in": punch_in,
                "punch_out": punch_out,
                "auto_closed": session.get("auto_closed", False),
                "duration_minutes": max(0, duration),
            }
        )

    status = record.get("status", AttendanceStatus.NOT_STARTED.value)
    holiday = record.get("holiday") or {}
    leave = record.get("leave") or {}

    return AttendanceDayOut(
        date=record["date"],
        status=status,
        status_label=_status_label(status, record["date"]),
        color=STATUS_COLORS.get(status, "grey"),
        worked_minutes=record.get("worked_minutes", 0),
        worked_display=format_minutes(record.get("worked_minutes", 0)),
        expected_minutes=record.get("expected_minutes", 0),
        shortfall_minutes=record.get("shortfall_minutes", 0),
        is_late=record.get("is_late", False),
        is_open=record.get("has_open_session", False),
        is_manually_adjusted=record.get("is_manually_adjusted", False),
        first_punch_in=record.get("first_punch_in"),
        last_punch_out=record.get("last_punch_out"),
        sessions=sessions,
        holiday_name=holiday.get("name"),
        leave_type=leave.get("leave_type_name"),
        leave_day_part=leave.get("day_part"),
        adjustment_note=record.get("adjustment_note"),
        user_id=record.get("user_id"),
        employee_name=record.get("employee_name"),
        employee_email=record.get("employee_email"),
        employee_code=record.get("employee_code"),
        department=record.get("department"),
        # Set by the register when it builds the row; absent everywhere else,
        # where it correctly stays False.
        no_shift_assigned=record.get("no_shift_assigned", False),
    )


# --------------------------------------------------------------------------- #
# punching
# --------------------------------------------------------------------------- #
async def _already_processed(user_id: str, request_key: str | None) -> bool:
    """Has this exact punch already been handled?

    Checked *before* any business rule. A retry of a punch that succeeded must
    come back as the same success, not as "you are already punched in" or "wait
    sixty seconds" — those answers would be about the first request, not this one.
    """
    if not request_key:
        return False
    return (
        await _punch_requests().find_one({"user_id": user_id, "request_key": request_key})
        is not None
    )


async def _claim_request_key(user_id: str, action: str, request_key: str | None) -> bool:
    """Reserve the key immediately before writing. False means a concurrent duplicate.

    Claimed late on purpose: a request rejected by validation has not happened,
    so its key must stay free for a corrected retry.
    """
    if not request_key:
        return True
    try:
        await _punch_requests().insert_one(
            PunchRequest(
                user_id=user_id,
                request_key=request_key,
                action=action,
                expires_at=utc_now() + timedelta(days=1),
            ).to_mongo()
        )
    except DuplicateKeyError:
        return False
    return True


async def _require_location(payload: PunchRequestBody) -> None:
    org_settings = await settings_service.get_settings()
    if not org_settings.get("geolocation_required", True):
        return
    if payload.location is None:
        raise ValidationError(
            "Your location is required to punch in or out. Allow location access "
            "in your browser and try again.",
            code="LOCATION_REQUIRED",
        )


async def _require_punch_permission(user: dict[str, Any], ip: str | None) -> None:
    """Both gates on punching: the account, then the network it punched from."""
    if not user_may_punch(user):
        raise PermissionDeniedError(
            "This account is not set up to punch in and out. Ask an "
            "administrator if you believe that is wrong.",
            code="PUNCH_NOT_PERMITTED",
        )
    await network.require_office_network(user, ip, gate=network.NetworkGate.PUNCH)


async def _punch_event(
    payload: PunchRequestBody,
    *,
    at: datetime,
    ip: str,
    user_agent: str,
    device_class: str,
    platform: str,
) -> dict[str, Any]:
    """One punch, with everything worth knowing about where it came from.

    The geofence flags and never blocks: a phone that put somebody in the next
    postcode is a bad GPS fix far more often than it is a fraudulent punch, and
    the cost of being wrong falls on the employee's attendance.
    """
    location = payload.location
    event: dict[str, Any] = {
        "at": at,
        "latitude": location.latitude if location else None,
        "longitude": location.longitude if location else None,
        "accuracy_metres": location.accuracy_metres if location else None,
        "address": location.address if location else None,
        "city": None,
        "distance_metres": None,
        "outside_office": False,
        "ip": ip,
        "user_agent": user_agent[:400] if user_agent else None,
        "device_class": device_class,
        "platform": platform,
        "source": PunchSource.WEB.value,
        "note": payload.note,
    }
    if location is None:
        return event

    org_settings = await settings_service.get_settings()
    office_latitude = org_settings.get("office_latitude")
    office_longitude = org_settings.get("office_longitude")
    if office_latitude is not None and office_longitude is not None:
        distance = geocode_service.distance_metres(
            location.latitude, location.longitude, office_latitude, office_longitude
        )
        event["distance_metres"] = round(distance)
        event["outside_office"] = distance > int(
            org_settings.get("office_radius_metres", 200)
        )

    # The browser's own address wins when it sent one, but it never sends a
    # city, and the city is what the register shows — so the lookup runs either
    # way. It answers an empty place rather than raising when geocoding is
    # unavailable, so a punch is never held up by it.
    place = await geocode_service.reverse_place(location.latitude, location.longitude)
    event["address"] = event["address"] or place.address
    event["city"] = place.city
    return event


async def ensure_day_document(
    user: dict[str, Any], day: date, shift: dict[str, Any] | None
) -> dict[str, Any]:
    day_key = date_key(day)
    existing = await _attendance().find_one({"user_id": user["id"], "date": day_key})
    if existing is not None:
        return existing

    record = Attendance(
        user_id=user["id"],
        date=day_key,
        shift_id=shift.get("id") if shift else None,
        shift_snapshot=shift,
    )
    with suppress(DuplicateKeyError):
        # A concurrent request may have created it first; theirs is just as good.
        await _attendance().insert_one(record.to_mongo())
    return await _attendance().find_one({"user_id": user["id"], "date": day_key})


async def punch_in(
    user: dict[str, Any],
    payload: PunchRequestBody,
    *,
    ip: str,
    user_agent: str,
    device_class: str,
    platform: str,
) -> dict[str, Any]:
    now = utc_now()
    day = attendance_date(now)

    if await _already_processed(user["id"], payload.request_key):
        logger.info("Replayed punch-in ignored for %s", user["email"])
        return await recompute_day(user["id"], day, user=user)

    await _require_punch_permission(user, ip)
    await _require_location(payload)

    shift = await shift_for_user(user)
    document = await ensure_day_document(user, day, shift)

    open_session = next(
        (s for s in _session_dicts(document) if s.get("punch_out") is None), None
    )
    if open_session is not None:
        raise ConflictError(
            "You are already punched in. Punch out before punching in again.",
            code="ALREADY_PUNCHED_IN",
        )

    org_settings = await settings_service.get_settings()
    sessions = _session_dicts(document)
    if sessions and not org_settings.get("allow_multiple_sessions", True):
        raise ConflictError(
            "Only one punch-in per day is allowed.", code="MULTIPLE_SESSIONS_DISABLED"
        )

    gap = int(org_settings.get("min_punch_gap_seconds", 60))
    if sessions and gap:
        last_out = sessions[-1].get("punch_out") or {}
        if last_out.get("at") and (now - to_utc(last_out["at"])).total_seconds() < gap:
            raise ConflictError(
                f"Please wait {gap} seconds between punches.", code="PUNCH_TOO_SOON"
            )

    if not await _claim_request_key(user["id"], "punch_in", payload.request_key):
        logger.info("Concurrent duplicate punch-in ignored for %s", user["email"])
        return await recompute_day(user["id"], day, user=user)

    event = await _punch_event(
        payload,
        at=now,
        ip=ip,
        user_agent=user_agent,
        device_class=device_class,
        platform=platform,
    )
    await _attendance().update_one(
        {"_id": document["_id"]},
        {
            "$push": {"sessions": {"punch_in": event, "punch_out": None, "auto_closed": False}},
            "$set": {"updated_at": now},
        },
    )
    logger.info("%s punched in at %s", user["email"], now.isoformat())
    return await recompute_day(user["id"], day, user=user)


async def punch_out(
    user: dict[str, Any],
    payload: PunchRequestBody,
    *,
    ip: str,
    user_agent: str,
    device_class: str,
    platform: str,
) -> dict[str, Any]:
    now = utc_now()
    day = attendance_date(now)

    if await _already_processed(user["id"], payload.request_key):
        logger.info("Replayed punch-out ignored for %s", user["email"])
        return await recompute_day(user["id"], day, user=user)

    await _require_punch_permission(user, ip)
    await _require_location(payload)

    document = await _attendance().find_one({"user_id": user["id"], "date": date_key(day)})

    open_index = None
    for index, session in enumerate(_session_dicts(document or {})):
        if session.get("punch_out") is None:
            open_index = index
            break

    if open_index is None:
        # Yesterday's session may still be open if they worked past midnight.
        previous = await _attendance().find_one(
            {
                "user_id": user["id"],
                "date": date_key(day - timedelta(days=1)),
                "sessions.punch_out": None,
            }
        )
        if previous is not None:
            document = previous
            day = day - timedelta(days=1)
            for index, session in enumerate(_session_dicts(document)):
                if session.get("punch_out") is None:
                    open_index = index
                    break

    if open_index is None:
        raise ConflictError(
            "You are not currently punched in.", code="NOT_PUNCHED_IN"
        )

    if not await _claim_request_key(user["id"], "punch_out", payload.request_key):
        logger.info("Concurrent duplicate punch-out ignored for %s", user["email"])
        return await recompute_day(user["id"], day, user=user)

    started = to_utc(_session_dicts(document)[open_index]["punch_in"]["at"])
    if now <= started:
        raise ValidationError(
            "Punch-out cannot be before punch-in.", code="INVALID_PUNCH_TIME"
        )

    event = await _punch_event(
        payload,
        at=now,
        ip=ip,
        user_agent=user_agent,
        device_class=device_class,
        platform=platform,
    )
    await _attendance().update_one(
        {"_id": document["_id"]},
        {"$set": {f"sessions.{open_index}.punch_out": event, "updated_at": now}},
    )
    logger.info("%s punched out at %s", user["email"], now.isoformat())
    return await recompute_day(user["id"], day, user=user)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def get_today(user: dict[str, Any], *, device_class: str = "UNKNOWN") -> dict[str, Any]:
    day = org_today()
    record = await recompute_day(user["id"], day, user=user)
    org_settings = await settings_service.get_settings()

    open_session = record.get("has_open_session", False)
    status = record.get("status")
    non_working = status in {
        AttendanceStatus.HOLIDAY.value,
        AttendanceStatus.WEEKEND.value,
        AttendanceStatus.ON_LEAVE.value,
    }

    may_punch = user_may_punch(user)

    blocked_reason = None
    if not may_punch:
        blocked_reason = "This account is not set up to punch in and out."
    elif non_working and not open_session:
        blocked_reason = {
            AttendanceStatus.HOLIDAY.value: "Today is a holiday — no punch is needed.",
            AttendanceStatus.WEEKEND.value: "Today is a weekly off — no punch is needed.",
            AttendanceStatus.ON_LEAVE.value: "You are on approved leave today.",
        }[status]

    elapsed = 0
    if open_session and record.get("first_punch_in"):
        last_open = next(
            (s for s in record.get("sessions", []) if s.get("punch_out") is None), None
        )
        if last_open:
            elapsed = int(
                (utc_now() - to_utc(last_open["punch_in"]["at"])).total_seconds() // 60
            )

    return {
        "server_time": utc_now(),
        "date": date_key(day),
        "day": to_day_out(record),
        "can_punch_in": not open_session and blocked_reason is None,
        "can_punch_out": open_session and may_punch,
        "blocked_reason": blocked_reason,
        # Deliberately *not* a `blocked_reason`: no shift assigned never stops
        # anybody punching. It is a note, because the person it actually
        # concerns is the administrator who planned the window — and an
        # employee who can see it can ask them about it.
        "no_shift_assigned": not shift_service.assignment_active(user, day),
        "shift": record.get("shift_snapshot"),
        "device_class": device_class,
        "geolocation_required": org_settings.get("geolocation_required", True),
        "elapsed_minutes": max(0, elapsed),
    }


async def month_calendar(user: dict[str, Any], year: int, month: int) -> MonthCalendarOut:
    """Every day of a month, computed — the calendar under the punch widget."""
    first, last = month_range(year, month)
    today = org_today()
    now = utc_now()

    shift = await shift_for_user(user)
    holidays = await holiday_service.holiday_map(first, last)
    leaves = await approved_leave_map(user["id"], first, last)
    rulings = {
        decision.date: decision.is_working
        for decision in await work_calendar_service.decisions_for_range(first, last, user=user)
    }

    cursor = _attendance().find(
        {"user_id": user["id"], "date": {"$gte": date_key(first), "$lte": date_key(last)}}
    )
    stored = {
        document["date"]: document for document in await cursor.to_list(length=40)
    }

    days: list[AttendanceDayOut] = []
    summary = MonthSummary()
    employed_from = employment_start(user)

    for day in iter_dates(first, last):
        day_key = date_key(day)
        document = stored.get(day_key)
        day_shift = (document or {}).get("shift_snapshot") or shift

        computed = compute_day_status(
            day,
            _session_dicts(document or {}),
            day_shift,
            today=today,
            now=now,
            holiday=holidays.get(day_key),
            leave=leaves.get(day_key),
            working_day=rulings.get(day_key),
            employed_from=employed_from,
        )

        record = serialize(document) if document else {"date": day_key, "sessions": []}
        record.update(
            {
                "date": day_key,
                "status": computed.status.value,
                "worked_minutes": computed.worked_minutes,
                "expected_minutes": computed.expected_minutes,
                "shortfall_minutes": computed.shortfall_minutes,
                "is_late": computed.is_late,
                "has_open_session": computed.has_open_session,
                "first_punch_in": computed.first_punch_in,
                "last_punch_out": computed.last_punch_out,
                "holiday": holidays.get(day_key),
                "leave": leaves.get(day_key),
            }
        )
        days.append(to_day_out(record))

        status = computed.status
        summary.worked_minutes += computed.worked_minutes
        if status is AttendanceStatus.PRESENT:
            summary.present_days += 1
        elif status is AttendanceStatus.HALF_DAY:
            summary.half_days += 1
        elif status in (AttendanceStatus.ABSENT, AttendanceStatus.SHORT_HOURS):
            summary.absent_days += 1
        elif status is AttendanceStatus.MISSING_PUNCH_OUT:
            summary.missing_punch_out_days += 1
        elif status is AttendanceStatus.ON_LEAVE:
            summary.leave_days += 1
        elif status is AttendanceStatus.HOLIDAY:
            summary.holidays += 1
        elif status is AttendanceStatus.WEEKEND:
            summary.weekly_offs += 1

        if computed.is_late:
            summary.late_days += 1
        if status not in (
            AttendanceStatus.HOLIDAY,
            AttendanceStatus.WEEKEND,
            AttendanceStatus.UPCOMING,
            AttendanceStatus.BEFORE_JOINING,
        ):
            summary.working_days += 1
            summary.expected_minutes += computed.expected_minutes

    summary.worked_display = format_minutes(summary.worked_minutes)

    return MonthCalendarOut(
        year=year,
        month=month,
        from_date=date_key(first),
        to_date=date_key(last),
        days=days,
        summary=summary,
    )


async def list_attendance(
    query: AttendanceQuery, *, requesting_user_id: str | None = None
) -> list[AttendanceDayOut]:
    """A flat range of stored days, for the admin register and history views."""
    mongo_query: dict[str, Any] = {}
    mongo_query["user_id"] = query.user_id or requesting_user_id
    if query.from_date or query.to_date:
        window: dict[str, str] = {}
        if query.from_date:
            window["$gte"] = date_key(query.from_date)
        if query.to_date:
            window["$lte"] = date_key(query.to_date)
        mongo_query["date"] = window
    if query.status:
        mongo_query["status"] = query.status.value

    cursor = _attendance().find(mongo_query).sort("date", -1).limit(400)
    documents = await cursor.to_list(length=400)

    results = []
    for document in documents:
        record = serialize(document)
        record["has_open_session"] = any(
            session.get("punch_out") is None for session in record.get("sessions", [])
        )
        results.append(to_day_out(record))
    return results


#: A register row is one employee on one day, so a span multiplies rather than
#: adds. A month is the longest stretch that is still a screen and not an export.
MAX_REGISTER_SPAN_DAYS = 31
MAX_REGISTER_EMPLOYEES = 1000
MAX_REGISTER_ROWS = 2000

#: Punching from one of these is a flag on the day, never a refusal of it.
EXCEPTION_DEVICE_CLASSES = {"MOBILE", "TABLET"}

#: Monday is 0, so anything below this is a weekday.
SATURDAY_WEEKDAY = 5

#: The day's own verdict that an administrator has to act on.
EXCEPTION_STATUSES = {AttendanceStatus.ABSENT, AttendanceStatus.MISSING_PUNCH_OUT}

#: Days nothing was expected on. Punch properties on them mean nothing.
NON_WORKING_STATUSES = {
    AttendanceStatus.WEEKEND,
    AttendanceStatus.HOLIDAY,
    AttendanceStatus.ON_LEAVE,
    AttendanceStatus.UPCOMING,
    AttendanceStatus.BEFORE_JOINING,
}


def _register_range(query: RegisterQuery) -> tuple[date, date]:
    """The span the register covers, or a refusal."""
    if query.from_date or query.to_date:
        if query.day is not None:
            raise ValidationError(
                "Ask for a single 'day' or a 'from_date'/'to_date' range, not both.",
                code="REGISTER_RANGE_AMBIGUOUS",
            )
        start = query.from_date or query.to_date
        end = query.to_date or query.from_date
        if start > end:
            raise ValidationError(
                "'from_date' must not be after 'to_date'.", code="REGISTER_RANGE_INVALID"
            )
        if (end - start).days + 1 > MAX_REGISTER_SPAN_DAYS:
            raise ValidationError(
                f"The register covers at most {MAX_REGISTER_SPAN_DAYS} days at a time. "
                "Narrow the range.",
                code="REGISTER_RANGE_TOO_LONG",
            )
        return start, end

    day = query.day or org_today()
    return day, day


async def _register_employees(
    search: str | None, department: str | None
) -> list[dict[str, Any]]:
    """The people the register is about — narrowed in the query, not afterwards."""
    mongo_query: dict[str, Any] = {"is_deleted": False, "status": "ACTIVE"}
    if department:
        mongo_query["department"] = department
    if search and search.strip():
        pattern = re.escape(search.strip())
        mongo_query["$or"] = [
            {"first_name": {"$regex": pattern, "$options": "i"}},
            {"last_name": {"$regex": pattern, "$options": "i"}},
            {"email": {"$regex": pattern, "$options": "i"}},
            {"employee_code": {"$regex": pattern, "$options": "i"}},
        ]
    cursor = _users().find(mongo_query)
    return [
        serialize(document)
        for document in await cursor.to_list(length=MAX_REGISTER_EMPLOYEES)
    ]


def _punch_events(record: dict[str, Any]):
    for session in record.get("sessions") or []:
        for event in (session.get("punch_in"), session.get("punch_out")):
            if event:
                yield event


def _status_label(status: str, day_key: str) -> str:
    """The label an employee reads, which is not always the status name.

    A weekday the office closed by hand stores the same status as a Sunday —
    both are simply "not a working day" — but calling a closed Wednesday a
    weekly off reads as a bug to anyone looking at it.
    """
    if status == AttendanceStatus.WEEKEND:
        weekday = date.fromisoformat(day_key).weekday()
        if weekday < SATURDAY_WEEKDAY:
            return "Office closed"
    return STATUS_LABELS.get(status, status)


def _needs_attention(record: dict[str, Any]) -> bool:
    """Is this a day somebody has to look at?

    Absence and a forgotten punch-out are the day's own verdict. Lateness, a
    punch from outside the office and a punch from a phone are properties of
    where the punches came from — none of them stop the day counting, which is
    precisely why they have to surface somewhere.
    """
    if record.get("status") in EXCEPTION_STATUSES:
        return True

    # A day nobody was expected to work is not an exception, whatever the
    # punches on it look like. Somebody who came in on a closed Saturday and
    # arrived "late" for a shift that was not running has done nothing that
    # needs chasing — they have earned a comp-off.
    if record.get("status") in NON_WORKING_STATUSES:
        return False

    if record.get("is_late"):
        return True

    # Somebody working a day with no shift planned for it. The punches are
    # perfectly valid — this is the planning signal the administrator asked for.
    if record.get("no_shift_assigned") and _punch_events(record):
        return True

    return any(
        event.get("outside_office")
        or event.get("device_class") in EXCEPTION_DEVICE_CLASSES
        for event in _punch_events(record)
    )


def _sort_register(
    rows: list[AttendanceDayOut], sort: RegisterSort | None, direction: SortDirection
) -> list[AttendanceDayOut]:
    """Order the rows a column header asked for.

    Sorted here rather than in the query because most rows never existed in the
    database: an absent day is the absence of a document, and it still has to
    take its place in the ordering.
    """
    if sort is None:
        return rows

    reverse = direction is SortDirection.DESC

    def by_name(row: AttendanceDayOut) -> tuple[str, str]:
        return ((row.employee_name or "").lower(), row.date)

    if sort is RegisterSort.EMPLOYEE:
        return sorted(rows, key=by_name, reverse=reverse)
    if sort is RegisterSort.WORKED_MINUTES:
        return sorted(rows, key=lambda row: (row.worked_minutes, by_name(row)), reverse=reverse)

    # A day nobody punched has no first punch, and "no time" is not an early
    # time — those rows sit at the end whichever way the column points.
    punched = [row for row in rows if row.first_punch_in]
    unpunched = sorted([row for row in rows if not row.first_punch_in], key=by_name)
    punched.sort(key=lambda row: (to_utc(row.first_punch_in), by_name(row)), reverse=reverse)
    return punched + unpunched


async def register(query: RegisterQuery) -> tuple[list[AttendanceDayOut], RegisterMeta]:
    """The admin's register: every employee's day, for one date or a short span.

    Status and the exception flags are decided here rather than by Mongo because
    a day nobody punched has no document to match. Absence *is* the missing row,
    so a filter that only saw stored documents would quietly drop exactly the
    days an administrator opened the screen to find.
    """
    start, end = _register_range(query)
    span_days = (end - start).days + 1
    employees = await _register_employees(query.search, query.department)

    today = org_today()
    now = utc_now()
    holidays = await holiday_service.holiday_map(start, end)

    stored: dict[tuple[str, str], dict[str, Any]] = {}
    if employees:
        cursor = _attendance().find(
            {
                "user_id": {"$in": [employee["id"] for employee in employees]},
                "date": {"$gte": date_key(start), "$lte": date_key(end)},
            }
        )
        for document in await cursor.to_list(length=len(employees) * span_days):
            stored[(document["user_id"], document["date"])] = document

    rows: list[AttendanceDayOut] = []
    matched = 0

    for user in employees:
        shift = await shift_for_user(user)
        leaves = await approved_leave_map(user["id"], start, end)
        rulings = {
            decision.date: decision.is_working
            for decision in await work_calendar_service.decisions_for_range(
                start, end, user=user
            )
        }
        name = (
            f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            or user["email"]
        )

        for day in iter_dates(start, end):
            day_key = date_key(day)
            document = stored.get((user["id"], day_key))
            day_shift = (document or {}).get("shift_snapshot") or shift

            computed = compute_day_status(
                day,
                _session_dicts(document or {}),
                day_shift,
                today=today,
                now=now,
                holiday=holidays.get(day_key),
                leave=leaves.get(day_key),
                working_day=rulings.get(day_key),
                employed_from=employment_start(user),
            )

            record = serialize(document) if document else {"date": day_key, "sessions": []}
            record.update(
                {
                    "date": day_key,
                    "status": computed.status.value,
                    "worked_minutes": computed.worked_minutes,
                    "expected_minutes": computed.expected_minutes,
                    "shortfall_minutes": computed.shortfall_minutes,
                    "is_late": computed.is_late,
                    "has_open_session": computed.has_open_session,
                    "first_punch_in": computed.first_punch_in,
                    "last_punch_out": computed.last_punch_out,
                    "holiday": holidays.get(day_key),
                    "leave": leaves.get(day_key),
                    "user_id": user["id"],
                    # A shift is a planned window for a piece of work. Nobody is
                    # stopped from punching without one, but the administrator
                    # who planned the window needs to see that it has run out
                    # while the work is still going.
                    #
                    # A *lapse*, not merely "no active assignment": an
                    # organisation that has never assigned a shift would
                    # otherwise have every punched day in its exception feed.
                    "no_shift_assigned": shift_service.assignment_lapsed(user, day),
                    "employee_name": name,
                    "employee_email": user.get("email"),
                    "employee_code": user.get("employee_code"),
                    "department": user.get("department"),
                }
            )

            if query.status is not None and computed.status != query.status:
                continue
            if query.exceptions_only and not _needs_attention(record):
                continue

            matched += 1
            if len(rows) < MAX_REGISTER_ROWS:
                rows.append(to_day_out(record))

    meta = RegisterMeta(
        from_date=date_key(start),
        to_date=date_key(end),
        days=span_days,
        employees=len(employees),
        total=matched,
        returned=len(rows),
        truncated=matched > len(rows),
        filtered=query.is_filtered,
    )
    return _sort_register(rows, query.sort, query.direction), meta


# --------------------------------------------------------------------------- #
# administrative adjustment
# --------------------------------------------------------------------------- #
async def adjust_day(
    user_id: str, day: date, payload: AttendanceAdjust, actor: Actor
) -> dict[str, Any]:
    """Replace a day's sessions wholesale. Always audited."""
    user = serialize(await _users().find_one({"_id": ObjectId(user_id), "is_deleted": False}))
    if user is None:
        raise NotFoundError("This employee was not found.", code="USER_NOT_FOUND")

    for session in payload.sessions:
        if attendance_date(to_utc(session.punch_in)) != day:
            raise ValidationError(
                f"Punch times must fall on {date_key(day)}.", code="SESSION_OUTSIDE_DAY"
            )

    shift = await shift_for_user(user)
    document = await ensure_day_document(user, day, shift)
    before = serialize(document)

    sessions = [
        {
            "punch_in": {
                "at": to_utc(session.punch_in),
                "source": PunchSource.ADMIN.value,
                "note": payload.note,
            },
            "punch_out": (
                {
                    "at": to_utc(session.punch_out),
                    "source": PunchSource.ADMIN.value,
                    "note": payload.note,
                }
                if session.punch_out
                else None
            ),
            "auto_closed": False,
        }
        for session in sorted(payload.sessions, key=lambda item: item.punch_in)
    ]

    await _attendance().update_one(
        {"_id": document["_id"]},
        {
            "$set": {
                "sessions": sessions,
                "is_manually_adjusted": True,
                "adjusted_by": actor.id,
                "adjusted_at": utc_now(),
                "adjustment_note": payload.note,
                "updated_at": utc_now(),
            }
        },
    )

    result = await recompute_day(user_id, day, user=user)
    await audit.record(
        actor,
        AuditAction.ADJUST,
        AuditEntity.ATTENDANCE,
        entity_id=str(document["_id"]),
        entity_label=f"{user['email']} {date_key(day)}",
        before={
            "status": before.get("status"),
            "worked_minutes": before.get("worked_minutes"),
            "session_count": len(before.get("sessions") or []),
        },
        after={
            "status": result.get("status"),
            "worked_minutes": result.get("worked_minutes"),
            "session_count": len(sessions),
            "note": payload.note,
        },
    )
    return result


# --------------------------------------------------------------------------- #
# scheduled maintenance
# --------------------------------------------------------------------------- #
async def close_stale_sessions(*, now: datetime | None = None) -> int:
    """Mark yesterday-and-earlier open sessions as forgotten punch-outs.

    No punch-out time is invented — the session is flagged ``auto_closed`` and the
    day becomes ``MISSING_PUNCH_OUT``, which is exactly the state a correction
    request is meant to resolve.
    """
    now = now or utc_now()
    today_key = date_key(attendance_date(now))

    cursor = _attendance().find(
        {"date": {"$lt": today_key}, "sessions.punch_out": None}
    )
    documents = await cursor.to_list(length=1000)

    closed = 0
    for document in documents:
        sessions = document.get("sessions") or []
        touched = False
        for session in sessions:
            if session.get("punch_out") is None and not session.get("auto_closed"):
                session["auto_closed"] = True
                touched = True
        if not touched:
            continue

        await _attendance().update_one(
            {"_id": document["_id"]},
            {"$set": {"sessions": sessions, "updated_at": now}},
        )
        await recompute_day(document["user_id"], date.fromisoformat(document["date"]))
        # Tell them the same evening. A forgotten punch-out found three days
        # later is a correction request; found tonight, it is a memory.
        await notification_service.notify(
            document["user_id"],
            type=NotificationType.MISSING_PUNCH_OUT,
            title="You did not punch out",
            body=(
                f"Your session on {document['date']} was left open, so the day counts as "
                "incomplete. File a correction and it will be put right."
            ),
            link="/corrections",
        )
        closed += 1

    if closed:
        logger.info("Auto-closed %d stale attendance day(s).", closed)
    return closed
