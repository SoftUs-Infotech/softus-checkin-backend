"""Leave: balances, applications, approval.

Two things this module is careful about:

* **Which days actually count.** Weekends (per the employee's shift) and holidays
  are stripped out when a request is created. A Friday-to-Monday request over a
  five-day week costs two days, not four, which is what an employee expects.
* **Reserving before approving.** Applying moves days into ``pending`` straight
  away. Without that, two overlapping requests could each pass the balance check
  and both be approved, overdrawing the account.
* **Crediting once.** Accrual is driven by a stored policy and recorded in a
  ledger on the balance, so the scheduled job can run twice — or be triggered by
  hand mid-cycle — without anybody earning the same days again.
* **Starting from go-live.** Once an administrator resets attendance, nothing
  before that day is earned, carried over or charged: every balance starts at
  zero on the go-live day and accrues from there.
"""

from contextlib import suppress
from datetime import date, timedelta
from typing import Any, NamedTuple

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.datetime import date_key, iter_dates, month_range, org_today, utc_now
from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import NotificationType
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.correction import ApprovalStatus
from app.models.leave import (
    DAY_PART_VALUE,
    AccrualCredit,
    DayPart,
    LeaveBalance,
    LeaveRequest,
)
from app.models.leave_type import (
    AccrualFrequency,
    AccrualPolicy,
    AccrualTiming,
    resolve_accrual_policy,
)
from app.models.user import UserStatus
from app.schemas.bulk import BulkDecision, BulkDecisionOut, BulkResult
from app.schemas.common import PaginationParams
from app.schemas.leave import LeaveApply, LeaveBalanceAdjust, LeaveDecision
from app.services import attendance as attendance_service
from app.services import audit
from app.services import holiday as holiday_service
from app.services import leave_type as leave_type_service
from app.services import notification as notification_service
from app.services import settings as settings_service
from app.services import user as user_service
from app.services.attendance_rules import is_working_weekday
from app.services.audit import Actor
from app.utils.email import templates

logger = get_logger(__name__)


def _requests():
    return get_collection(Collections.LEAVE_REQUESTS)


def _balances():
    return get_collection(Collections.LEAVE_BALANCES)


def _users():
    return get_collection(Collections.USERS)


def _salary_adjustments():
    return get_collection(Collections.SALARY_ADJUSTMENTS)


def _display_name(user: dict[str, Any]) -> str:
    return (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("email", "")
    )


# --------------------------------------------------------------------------- #
# balances
# --------------------------------------------------------------------------- #
class AccrualCycle(NamedTuple):
    """One turn of an accrual policy, and the day its credit falls due."""

    key: str
    start: date
    end: date
    credit_on: date


def cycles_in_year(policy: AccrualPolicy, year: int) -> list[AccrualCycle]:
    """Every cycle of a calendar year, in order.

    Leave years are calendar years here because balances are keyed by year
    everywhere else. A policy that credits at the end of a cycle falls due on
    that cycle's last day, not on the first day of the next one.
    """
    spans: list[tuple[str, date, date]] = []
    if policy.frequency == AccrualFrequency.MONTHLY:
        for month in range(1, 13):
            first, last = month_range(year, month)
            spans.append((f"{year}-M{month:02d}", first, last))
    elif policy.frequency == AccrualFrequency.QUARTERLY:
        for quarter in range(1, 5):
            first, _ = month_range(year, quarter * 3 - 2)
            _, last = month_range(year, quarter * 3)
            spans.append((f"{year}-Q{quarter}", first, last))
    else:
        spans.append((f"{year}-Y", date(year, 1, 1), date(year, 12, 31)))

    at_end = policy.credit_at == AccrualTiming.END
    return [
        AccrualCycle(key, first, last, last if at_end else first)
        for key, first, last in spans
    ]


def _earning_from(
    user: dict[str, Any], leave_type: dict[str, Any], tracking_from: date | None = None
) -> date | None:
    """The first day this employee earns this type.

    Joining plus the waiting period, but never before go-live: days before the
    system was in use earn nothing, so balances start at zero on that day. A
    record with no joining date gives nothing to wait from or prorate against,
    so the employee is treated as having been here all along — better than
    silently crediting nobody.
    """
    earning: date | None = None
    joining = user.get("joining_date")
    if joining:
        joined = date.fromisoformat(joining) if isinstance(joining, str) else joining
        earning = joined + timedelta(days=int(leave_type.get("applicable_after_days", 0)))
    if tracking_from and (earning is None or earning < tracking_from):
        return tracking_from
    return earning


def _counts(request: dict[str, Any], tracking_from: date | None) -> bool:
    """Whether a request is charged to a balance.

    A request that starts before go-live belongs to the time before the system
    counted anything; balances were rebuilt without it, so deciding or
    withdrawing it later must not touch them either.
    """
    return tracking_from is None or request["from_date"] >= date_key(tracking_from)


def cycle_span(key: str) -> tuple[date, date] | None:
    """The days a ledger entry paid for, read back from its cycle key."""
    year_part, _, part = key.partition("-")
    try:
        year = int(year_part)
        if part == "Y":
            return date(year, 1, 1), date(year, 12, 31)
        if part.startswith("Q"):
            quarter = int(part[1:])
            first, _ = month_range(year, quarter * 3 - 2)
            _, last = month_range(year, quarter * 3)
            return first, last
        if part.startswith("M"):
            return month_range(year, int(part[1:]))
    except ValueError:
        pass
    return None


def cycle_credit(
    policy: AccrualPolicy,
    cycle: AccrualCycle,
    *,
    earning_from: date | None,
    covered: list[tuple[date, date]] | None = None,
) -> float:
    """A cycle's credit, prorated for the part of it nobody has paid for yet.

    Two things shrink it: joining (or go-live) partway through, and days an
    earlier credit already paid for. The second is what lets an administrator
    change a type from yearly to monthly mid-year: the year was credited in
    January, so the monthly cycles that follow add nothing until next year,
    rather than paying the same months twice.
    """
    amount = float(policy.amount_per_cycle)
    start = max(cycle.start, earning_from) if earning_from else cycle.start
    if start > cycle.end:
        return 0.0
    whole = (cycle.end - cycle.start).days + 1
    unpaid = (cycle.end - start).days + 1
    for first, last in covered or []:
        overlap = (min(last, cycle.end) - max(first, start)).days + 1
        if overlap > 0:
            unpaid -= overlap
    if unpaid <= 0:
        return 0.0
    return round(amount * unpaid / whole, 2)


async def _carried_forward(
    user_id: str, leave_type: dict[str, Any], year: int
) -> float:
    """What last year's leftover is worth this year.

    The cap wins wherever there is one; without a cap only a type that does not
    lapse keeps its leftover whole.
    """
    tracking_from = await settings_service.tracking_start()
    if tracking_from and year <= tracking_from.year:
        return 0.0  # last year was entirely before go-live

    previous = await _balances().find_one(
        {"user_id": user_id, "leave_type_id": leave_type["id"], "year": year - 1}
    )
    if previous is None:
        return 0.0

    leftover = (
        float(previous.get("allocated", 0))
        + float(previous.get("carried_forward", 0))
        - float(previous.get("used", 0))
        - float(previous.get("pending", 0))
    )
    if leftover <= 0:
        return 0.0

    cap = float(leave_type.get("carry_forward_max") or 0)
    policy = resolve_accrual_policy(leave_type)
    if cap or policy.lapse_at_year_end:
        return round(min(leftover, cap), 2)
    return round(leftover, 2)


async def _ensure_balance(
    user_id: str, leave_type: dict[str, Any], year: int
) -> dict[str, Any]:
    query = {"user_id": user_id, "leave_type_id": leave_type["id"], "year": year}
    existing = await _balances().find_one(query)
    if existing is not None:
        return serialize(existing)

    balance = LeaveBalance(
        user_id=user_id,
        leave_type_id=leave_type["id"],
        year=year,
        carried_forward=await _carried_forward(user_id, leave_type, year),
    )
    with suppress(DuplicateKeyError):  # another request got there first
        await _balances().insert_one(balance.to_mongo())
    return serialize(await _balances().find_one(query))


async def apply_due_credits(
    user: dict[str, Any],
    leave_type: dict[str, Any],
    year: int,
    *,
    as_of: date | None = None,
    notify: bool = False,
) -> float:
    """Credit every cycle that has fallen due and is not already in the ledger.

    The ``$ne`` guard on the cycle key is the whole idempotency story: a cycle
    already recorded matches no document, so a repeated run updates nothing and
    nobody is credited twice.
    """
    as_of = as_of or org_today()
    policy = resolve_accrual_policy(leave_type)
    earning_from = _earning_from(
        user, leave_type, await settings_service.tracking_start()
    )
    query = {"user_id": user["id"], "leave_type_id": leave_type["id"], "year": year}

    balance = await _balances().find_one(query, {"accrual_credits.cycle_key": 1}) or {}
    paid_keys = {credit["cycle_key"] for credit in balance.get("accrual_credits", [])}

    credited = 0.0
    for cycle in cycles_in_year(policy, year):
        if cycle.credit_on > as_of:
            break

        # Spans paid under a different frequency. Same-frequency cycles never
        # overlap, and this cycle's own key is guarded by the update below.
        covered = [
            span
            for key in paid_keys
            if key != cycle.key and (span := cycle_span(key)) is not None
        ]
        days = cycle_credit(policy, cycle, earning_from=earning_from, covered=covered)
        result = await _balances().update_one(
            {**query, "accrual_credits.cycle_key": {"$ne": cycle.key}},
            {
                "$inc": {"allocated": days},
                "$push": {
                    "accrual_credits": AccrualCredit(
                        cycle_key=cycle.key, days=days
                    ).model_dump()
                },
                "$set": {"updated_at": utc_now()},
            },
        )
        if result.modified_count == 0 or days <= 0:
            continue

        credited = round(credited + days, 2)
        if notify:
            await notification_service.notify(
                user["id"],
                type=NotificationType.GENERAL,
                title=f"{days:g} day(s) of {leave_type['name']} credited",
                body=f"Leave accrued for {cycle.key}.",
                link="/leave",
            )
    return credited


async def get_or_create_balance(
    user_id: str, leave_type: dict[str, Any], year: int
) -> dict[str, Any]:
    """The balance as it stands, with everything already earned credited to it.

    Reading brings the balance up to date so nobody sees a stale figure between
    scheduled runs. Those catch-up credits are silent: the notification belongs
    to the job, not to whoever happened to open the page.
    """
    await _ensure_balance(user_id, leave_type, year)
    user = serialize(await _users().find_one({"_id": ObjectId(user_id)})) or {
        "id": user_id
    }
    await apply_due_credits(user, leave_type, year)
    return serialize(
        await _balances().find_one(
            {"user_id": user_id, "leave_type_id": leave_type["id"], "year": year}
        )
    )


async def run_accruals(*, as_of: date | None = None) -> int:
    """Credit every active employee for every cycle that has turned.

    Idempotency lives in each balance's ledger rather than in a record of job
    runs, so a retried or duplicated run is a no-op rather than a windfall.
    Returns how many balances were actually credited.
    """
    as_of = as_of or org_today()
    leave_types = await leave_type_service.list_leave_types()
    if not leave_types:
        return 0

    documents = (
        await _users()
        .find({"status": UserStatus.ACTIVE.value, "is_deleted": False})
        .to_list(length=None)
    )

    credited = 0
    for document in documents:
        user = serialize(document)
        for leave_type in leave_types:
            await _ensure_balance(user["id"], leave_type, as_of.year)
            if await apply_due_credits(
                user,
                leave_type,
                as_of.year,
                as_of=as_of,
                # A hidden type still accrues, silently.
                notify=leave_type_service.visible_to(user, leave_type),
            ):
                credited += 1

    return credited


async def restart_balances(starts_on: date) -> int:
    """Rebuild every balance from go-live: zero on the day, accruing after it.

    Called when an administrator resets attendance. Days earned before, carried
    over into the go-live year or set by hand are dropped, and leave is charged
    again only for requests that start on or after the day. Earlier years are
    history and stay as they were. Accrual itself is not run here: the next read
    or scheduled run credits from go-live, prorated like a mid-cycle joining.
    Returns how many balances were rebuilt.
    """
    start_key = date_key(starts_on)
    leave_types = {
        leave_type["id"]: leave_type
        for leave_type in await leave_type_service.list_leave_types(include_inactive=True)
    }
    documents = (
        await _balances()
        .find({"year": {"$gte": starts_on.year}})
        .sort("year", 1)  # a later year carries over from the rebuilt one before it
        .to_list(length=None)
    )

    for document in documents:
        year = int(document["year"])
        charged = {ApprovalStatus.APPROVED.value: 0.0, ApprovalStatus.PENDING.value: 0.0}
        requests = _requests().find(
            {
                "user_id": document["user_id"],
                "leave_type_id": document["leave_type_id"],
                "is_deleted": False,
                "status": {"$in": list(charged)},
                "from_date": {"$gte": max(start_key, f"{year}-01-01"), "$lte": f"{year}-12-31"},
            }
        )
        async for request in requests:
            charged[request["status"]] += float(request.get("total_days", 0))

        leave_type = leave_types.get(str(document["leave_type_id"]))
        carried = (
            await _carried_forward(str(document["user_id"]), leave_type, year)
            if leave_type
            else 0.0
        )
        await _balances().update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "allocated": 0,
                    "accrual_credits": [],
                    "carried_forward": carried,
                    "used": round(charged[ApprovalStatus.APPROVED.value], 2),
                    "pending": round(charged[ApprovalStatus.PENDING.value], 2),
                    "updated_at": utc_now(),
                }
            },
        )

    logger.info("Leave balances restarted from %s: %d rebuilt.", start_key, len(documents))
    return len(documents)


async def list_balances(
    user_id: str, year: int | None = None, *, for_user: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Balances for every leave type, or only those ``for_user`` may see."""
    year = year or org_today().year
    leave_types = await leave_type_service.list_leave_types(for_user=for_user)

    results = []
    for leave_type in leave_types:
        balance = await get_or_create_balance(user_id, leave_type, year)
        allocated = float(balance.get("allocated", 0))
        carried = float(balance.get("carried_forward", 0))
        used = float(balance.get("used", 0))
        pending = float(balance.get("pending", 0))
        results.append(
            {
                "leave_type_id": leave_type["id"],
                "leave_type_name": leave_type["name"],
                "leave_type_code": leave_type["code"],
                "color": leave_type.get("color", "#2563eb"),
                "is_paid": leave_type.get("is_paid", True),
                "allow_half_day": leave_type.get("allow_half_day", True),
                "allow_negative_balance": leave_type.get("allow_negative_balance", False),
                "visible_to_employees": leave_type.get("visible_to_employees", True),
                "year": year,
                "allocated": allocated,
                "carried_forward": carried,
                "used": used,
                "pending": pending,
                "available": round(allocated + carried - used - pending, 2),
            }
        )
    return results


async def _move_balance(
    user_id: str, leave_type_id: str, year: int, *, pending: float = 0, used: float = 0
) -> None:
    await _balances().update_one(
        {"user_id": user_id, "leave_type_id": leave_type_id, "year": year},
        {"$inc": {"pending": pending, "used": used}, "$set": {"updated_at": utc_now()}},
    )


async def adjust_balance(
    user_id: str, leave_type_id: str, payload: LeaveBalanceAdjust, actor: Actor
) -> dict[str, Any]:
    leave_type = await leave_type_service.require_leave_type(leave_type_id)
    year = org_today().year
    before = await get_or_create_balance(user_id, leave_type, year)

    changes = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None and key != "note"
    }
    if not changes:
        return before

    changes["updated_at"] = utc_now()
    await _balances().update_one(
        {"user_id": user_id, "leave_type_id": leave_type_id, "year": year},
        {"$set": changes},
    )
    after = await get_or_create_balance(user_id, leave_type, year)

    await audit.record(
        actor,
        AuditAction.ADJUST,
        AuditEntity.LEAVE_BALANCE,
        entity_id=f"{user_id}:{leave_type_id}:{year}",
        entity_label=leave_type["name"],
        before={key: before.get(key) for key in changes if key != "updated_at"},
        after={**{key: after.get(key) for key in changes if key != "updated_at"},
               "note": payload.note},
    )
    return after


# --------------------------------------------------------------------------- #
# which days count
# --------------------------------------------------------------------------- #
async def resolve_leave_days(
    user: dict[str, Any], payload: LeaveApply
) -> tuple[list[dict[str, Any]], float]:
    """Strip weekends and holidays, then price what remains."""
    shift = await attendance_service.shift_for_user(user)
    holidays = await holiday_service.holiday_map(payload.from_date, payload.to_date)

    days: list[dict[str, Any]] = []
    for day in iter_dates(payload.from_date, payload.to_date):
        day_key = date_key(day)
        if day_key in holidays:
            continue
        if not is_working_weekday(day, shift):
            continue
        days.append({"date": day_key, "day_part": payload.day_part.value})

    total = round(sum(DAY_PART_VALUE[day["day_part"]] for day in days), 2)
    return days, total


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
async def _check_policy(
    user: dict[str, Any], leave_type: dict[str, Any], payload: LeaveApply, total_days: float
) -> None:
    if payload.day_part is not DayPart.FULL and not leave_type.get("allow_half_day", True):
        raise ValidationError(
            f"{leave_type['name']} cannot be taken as a half day.", code="HALF_DAY_NOT_ALLOWED"
        )

    notice = int(leave_type.get("min_notice_days", 0))
    if notice:
        days_ahead = (payload.from_date - org_today()).days
        if days_ahead < notice:
            raise ValidationError(
                f"{leave_type['name']} needs at least {notice} days' notice.",
                code="INSUFFICIENT_NOTICE",
                details={"required_days": notice, "given_days": days_ahead},
            )

    maximum = leave_type.get("max_consecutive_days")
    if maximum and total_days > float(maximum):
        raise ValidationError(
            f"{leave_type['name']} allows at most {maximum} consecutive days.",
            code="TOO_MANY_CONSECUTIVE_DAYS",
        )

    probation = int(leave_type.get("applicable_after_days", 0))
    joining = user.get("joining_date")
    if probation and joining:
        joined = date.fromisoformat(joining) if isinstance(joining, str) else joining
        served = (org_today() - joined).days
        if served < probation:
            raise ValidationError(
                f"{leave_type['name']} becomes available {probation} days after joining.",
                code="NOT_YET_ELIGIBLE",
            )


async def _check_overlap(user_id: str, day_keys: set[str]) -> None:
    cursor = _requests().find(
        {
            "user_id": user_id,
            "status": {"$in": [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]},
            "is_deleted": False,
            "days.date": {"$in": sorted(day_keys)},
        }
    )
    clash = await cursor.to_list(length=1)
    if clash:
        overlapping = sorted(
            {day["date"] for day in clash[0].get("days", [])} & day_keys
        )
        raise ConflictError(
            "You already have leave requested or approved on "
            f"{', '.join(overlapping[:3])}"
            + ("…" if len(overlapping) > 3 else "")
            + ".",
            code="LEAVE_OVERLAP",
            details={"dates": overlapping},
        )


async def apply_for_leave(
    user: dict[str, Any], payload: LeaveApply, actor: Actor
) -> dict[str, Any]:
    leave_type = await leave_type_service.require_leave_type(payload.leave_type_id)
    if not leave_type_service.visible_to(user, leave_type):
        # Indistinguishable from a type that does not exist: a hidden type is
        # not something an employee should learn about from an error message.
        raise NotFoundError("Leave type not found.", code="LEAVE_TYPE_NOT_FOUND")
    if not leave_type.get("is_active", True):
        raise ValidationError(
            f"{leave_type['name']} is no longer available.", code="LEAVE_TYPE_INACTIVE"
        )

    tracking_from = await settings_service.tracking_start()
    if tracking_from and payload.from_date < tracking_from:
        raise ValidationError(
            f"Leave can only be requested from {date_key(tracking_from)}, "
            "the day attendance started being tracked.",
            code="BEFORE_TRACKING_START",
        )

    days, total = await resolve_leave_days(user, payload)
    if not days:
        raise ValidationError(
            "That range contains no working days — it is all weekends or holidays.",
            code="NO_WORKING_DAYS",
        )

    await _check_policy(user, leave_type, payload, total)
    await _check_overlap(user["id"], {day["date"] for day in days})

    year = payload.from_date.year
    balance = await get_or_create_balance(user["id"], leave_type, year)
    available = (
        float(balance.get("allocated", 0))
        + float(balance.get("carried_forward", 0))
        - float(balance.get("used", 0))
        - float(balance.get("pending", 0))
    )
    # Unpaid leave has no quota to exhaust, and a type may be allowed to go
    # overdrawn — an advance against days not yet accrued.
    if (
        leave_type.get("is_paid", True)
        and not leave_type.get("allow_negative_balance", False)
        and total > available
    ):
        raise ValidationError(
            f"You have {available:g} day(s) of {leave_type['name']} left, "
            f"but requested {total:g}.",
            code="INSUFFICIENT_BALANCE",
            details={"available": available, "requested": total},
        )

    request = LeaveRequest(
        user_id=user["id"],
        user_email=user.get("email"),
        user_name=_display_name(user),
        leave_type_id=leave_type["id"],
        leave_type_name=leave_type["name"],
        leave_type_code=leave_type["code"],
        from_date=date_key(payload.from_date),
        to_date=date_key(payload.to_date),
        days=days,
        total_days=total,
        reason=payload.reason,
        contact_number=payload.contact_number,
    )
    result = await _requests().insert_one(request.to_mongo())

    # Reserve immediately so a second overlapping request cannot also pass.
    await _move_balance(user["id"], leave_type["id"], year, pending=total)

    created = await require_request(str(result.inserted_id))
    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.LEAVE_REQUEST,
        entity_id=created["id"],
        entity_label=f"{user.get('email')} {created['from_date']}→{created['to_date']}",
        after={
            "leave_type": leave_type["name"],
            "total_days": total,
            "from": created["from_date"],
            "to": created["to_date"],
        },
    )
    await notification_service.notify_approvers(
        user["id"],
        type=NotificationType.APPROVAL_PENDING,
        title="Leave request awaiting approval",
        body=f"{_display_name(user)} requested {total:g} day(s) of {leave_type['name']}.",
        link="/admin/leave",
    )
    logger.info(
        "%s applied for %g day(s) of %s", user.get("email"), total, leave_type["name"]
    )
    return created


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
async def require_request(request_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(request_id):
        raise NotFoundError("Leave request not found.", code="LEAVE_NOT_FOUND")
    document = await _requests().find_one(
        {"_id": ObjectId(request_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Leave request not found.", code="LEAVE_NOT_FOUND")
    return serialize(document)


async def list_requests(
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

    total = await _requests().count_documents(query)
    cursor = (
        _requests()
        .find(query)
        .sort("created_at", -1)
        .skip(pagination.skip)
        .limit(pagination.limit)
    )
    items = [serialize(document) for document in await cursor.to_list(length=pagination.limit)]
    return items, total


async def pending_count(approver: dict[str, Any] | None = None) -> int:
    # No approver means the whole organisation — see ``correction.pending_count``.
    query: dict[str, Any] = {"status": ApprovalStatus.PENDING.value, "is_deleted": False}
    if approver is not None:
        query.update(await user_service.approval_scope(approver, None))
    return await _requests().count_documents(query)


async def on_leave_today() -> list[dict[str, Any]]:
    """Who is away today — for the dashboard's right-hand rail."""
    today_key = date_key(org_today())
    cursor = _requests().find(
        {
            "status": ApprovalStatus.APPROVED.value,
            "is_deleted": False,
            "days.date": today_key,
        }
    )
    return [
        {
            "user_id": document["user_id"],
            "user_name": document.get("user_name"),
            "leave_type_name": document.get("leave_type_name"),
            "day_part": next(
                (
                    day["day_part"]
                    for day in document.get("days", [])
                    if day["date"] == today_key
                ),
                DayPart.FULL.value,
            ),
        }
        for document in await cursor.to_list(length=200)
    ]


# --------------------------------------------------------------------------- #
# late approvals
# --------------------------------------------------------------------------- #
def _month_key(day_key: str) -> str:
    return day_key[:7]


def _months_apart(earlier: str, later: str) -> int:
    return (int(later[:4]) - int(earlier[:4])) * 12 + int(later[5:7]) - int(earlier[5:7])


def _past_months(request: dict[str, Any], this_month: str) -> dict[str, list[dict]]:
    """The request's days grouped by month, keeping only months already closed."""
    grouped: dict[str, list[dict]] = {}
    for day in request.get("days", []):
        month = _month_key(day["date"])
        if month < this_month:
            grouped.setdefault(month, []).append(day)
    return grouped


async def _guard_retro_window(request: dict[str, Any], this_month: str) -> None:
    """Refuse an approval that reaches further back than policy allows.

    Past that horizon the month's payout has been settled for good, and quietly
    approving would leave the calendar and the payslip disagreeing.
    """
    months = _past_months(request, this_month)
    if not months:
        return

    oldest = min(months)
    allowed = int((await settings_service.get_settings()).get("retro_approval_months", 3))
    behind = _months_apart(oldest, this_month)
    if behind > allowed:
        raise ValidationError(
            f"This leave is from {oldest}, {behind} month(s) back, and approvals "
            f"may only reach {allowed} month(s) into the past. Adjust the balance "
            "and the salary line by hand instead.",
            code="RETRO_APPROVAL_TOO_OLD",
            details={
                "source_month": oldest,
                "months_back": behind,
                "allowed_months": allowed,
            },
        )


async def _post_retro_salary_adjustments(
    request: dict[str, Any], leave_type: dict[str, Any], approver: dict[str, Any]
) -> None:
    """Settle a late approval in the current month, one line per month corrected.

    A closed month's payout is never rewritten; the difference the approval makes
    is posted against the month being run now. Days only — the salary service
    turns them into money at that month's day rate, and it is the only place that
    knows the rate.
    """
    today = org_today()
    this_month = _month_key(date_key(today))
    months = _past_months(request, this_month)
    if not months:
        return

    org = await settings_service.get_settings()
    # While the request sat pending those days stood as absences. Only the
    # change in what gets deducted is owed: an unpaid type that is deducted
    # either way owes nothing at all.
    deducted_before = bool(org.get("salary_deduct_absent", True))
    deducted_after = not leave_type.get("is_paid", True) and bool(
        org.get("salary_deduct_unpaid_leave", True)
    )
    direction = int(deducted_before) - int(deducted_after)
    if not direction:
        return

    now = utc_now()
    documents = []
    for month, days in sorted(months.items()):
        total = round(
            sum(DAY_PART_VALUE[day["day_part"]] for day in days) * direction, 2
        )
        documents.append(
            {
                "user_id": ObjectId(request["user_id"]),
                "apply_month": this_month,
                "source_month": month,
                "source_date": min(day["date"] for day in days),
                "kind": "LEAVE_APPROVED_LATE",
                "days": total,
                "reason": (
                    f"{request.get('leave_type_name')} for {month} approved on "
                    f"{date_key(today)} by {approver.get('email')}."
                ),
                "reference_type": "leave_request",
                "reference_id": ObjectId(request["id"]),
                "created_by": ObjectId(approver["id"]),
                "created_at": now,
            }
        )

    await _salary_adjustments().insert_many(documents)
    logger.info(
        "Late approval of leave %s posted %d salary adjustment(s) into %s.",
        request["id"],
        len(documents),
        this_month,
    )


# --------------------------------------------------------------------------- #
# deciding
# --------------------------------------------------------------------------- #
async def decide(
    request_id: str,
    approve: bool,
    payload: LeaveDecision,
    approver: dict[str, Any],
    actor: Actor,
) -> dict[str, Any]:
    request = await require_request(request_id)
    await user_service.require_approval_authority(
        approver,
        request["user_id"],
        own_message="You cannot decide your own leave request.",
        own_code="CANNOT_DECIDE_OWN_LEAVE",
    )
    if request["status"] != ApprovalStatus.PENDING.value:
        raise ConflictError(
            f"This request was already {request['status'].lower()}.",
            code="LEAVE_ALREADY_DECIDED",
        )

    this_month = _month_key(date_key(org_today()))
    leave_type: dict[str, Any] | None = None
    if approve:
        # Everything that can refuse the approval runs before anything moves —
        # a refusal must leave the balance and the calendar exactly as they were.
        await _guard_retro_window(request, this_month)
        leave_type = await leave_type_service.require_leave_type(
            request["leave_type_id"]
        )

    year = int(request["from_date"][:4])
    total = float(request["total_days"])
    now = utc_now()

    if not _counts(request, await settings_service.tracking_start()):
        pass  # from before go-live: never reserved, so nothing to move
    elif approve:
        # Move the reservation into used.
        await _move_balance(
            request["user_id"], request["leave_type_id"], year, pending=-total, used=total
        )
    else:
        await _move_balance(
            request["user_id"], request["leave_type_id"], year, pending=-total
        )

    await _requests().update_one(
        {"_id": ObjectId(request_id)},
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

    if approve:
        # The calendar reads approved leave, so recompute the affected days —
        # including days in a month that has already been paid.
        for day in request.get("days", []):
            await attendance_service.recompute_day(
                request["user_id"], date.fromisoformat(day["date"])
            )
        await _post_retro_salary_adjustments(request, leave_type, approver)

    updated = await require_request(request_id)
    await audit.record(
        actor,
        AuditAction.APPROVE if approve else AuditAction.REJECT,
        AuditEntity.LEAVE_REQUEST,
        entity_id=request_id,
        entity_label=f"{request.get('user_email')} {request['from_date']}",
        before={"status": ApprovalStatus.PENDING.value},
        after={"status": updated["status"], "note": payload.note},
    )
    await notification_service.notify(
        request["user_id"],
        type=NotificationType.LEAVE_DECIDED,
        title=f"Your {request.get('leave_type_name')} request was "
        f"{'approved' if approve else 'rejected'}",
        body=payload.note
        or f"{request['from_date']} to {request['to_date']} ({total:g} day(s)).",
        link="/leave",
        # A refusal is the one that has to travel: those dates go back to being
        # working days, and somebody who never opens the app will be marked
        # absent for a week they believed was approved.
        email=(
            templates.leave_decision_email(
                to=request["user_email"],
                name=(request.get("user_name") or "").split(" ")[0],
                leave_type=request.get("leave_type_name") or "leave",
                from_date=request["from_date"],
                to_date=request["to_date"],
                total_days=total,
                approved=approve,
                note=payload.note,
            )
            if request.get("user_email")
            else None
        ),
    )
    logger.info(
        "Leave %s %s by %s", request_id, "approved" if approve else "rejected",
        approver.get("email"),
    )
    return updated


async def cancel(request_id: str, user: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """Withdraw a request. Approved future leave can also be withdrawn."""
    request = await require_request(request_id)
    if request["user_id"] != user["id"]:
        raise NotFoundError("Leave request not found.", code="LEAVE_NOT_FOUND")
    if request["status"] in (ApprovalStatus.REJECTED.value, ApprovalStatus.CANCELLED.value):
        raise ConflictError(
            "This request is already closed.", code="LEAVE_ALREADY_DECIDED"
        )

    first_day = date.fromisoformat(request["from_date"])
    if request["status"] == ApprovalStatus.APPROVED.value and first_day <= org_today():
        raise ConflictError(
            "Leave that has already started cannot be withdrawn. Ask an administrator.",
            code="LEAVE_ALREADY_STARTED",
        )

    year = int(request["from_date"][:4])
    total = float(request["total_days"])
    if not _counts(request, await settings_service.tracking_start()):
        pass  # from before go-live: never charged, so nothing to give back
    elif request["status"] == ApprovalStatus.PENDING.value:
        await _move_balance(
            request["user_id"], request["leave_type_id"], year, pending=-total
        )
    else:
        await _move_balance(request["user_id"], request["leave_type_id"], year, used=-total)

    await _requests().update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": ApprovalStatus.CANCELLED.value, "updated_at": utc_now()}},
    )

    for day in request.get("days", []):
        await attendance_service.recompute_day(
            request["user_id"], date.fromisoformat(day["date"])
        )

    updated = await require_request(request_id)
    await audit.record(
        actor,
        AuditAction.UPDATE,
        AuditEntity.LEAVE_REQUEST,
        entity_id=request_id,
        entity_label=f"{user.get('email')} {request['from_date']}",
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
    decision = LeaveDecision(note=payload.note)
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
