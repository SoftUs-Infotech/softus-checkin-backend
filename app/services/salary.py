"""Salary: history, and the projected month-end figure attendance implies.

Administrators only. Nothing in this module is reachable from an employee route,
and no employee-facing schema carries a field it produces — see the note on the
salary router.

Two ideas hold the whole thing up:

*A salary is a history.* ``salary_in_force`` picks the row that applied to the
month being computed, so a March raise cannot rewrite what February meant. The
figure in force on the **last** day of the month is the one used: a raise dated
mid-month is a raise for that month.

*An adjustment is days, not money.* A late-approved August leave says "give one
day back"; what that day is worth is decided here, from August's working-day
count and the CTC in force then. An amount stored at approval time would be a
number frozen against rules that can still change.

The top section is pure arithmetic and is unit-testable without a database.
"""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from bson import ObjectId

from app.core.datetime import date_key, month_range, org_today
from app.core.exceptions import ValidationError
from app.db.mongo import Collections, get_collection
from app.models.attendance import AttendanceStatus
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.correction import ApprovalStatus
from app.models.salary import Salary, SalaryAdjustment, SalaryAdjustmentKind
from app.models.settings import SalaryDayBasis
from app.models.user import EmploymentType, UserStatus
from app.schemas.salary import SalaryCreate
from app.services import audit
from app.services import holiday as holiday_service
from app.services import settings as settings_service
from app.services import shift as shift_service
from app.services import user as user_service
from app.services import work_calendar as calendar_service
from app.services.attendance_rules import (
    DEFAULT_REQUIRED_MINUTES,
    employment_start,
)
from app.services.audit import Actor

#: Statuses that mean the day was worked and nothing is owed back.
_WORKED_STATUSES = {
    AttendanceStatus.PRESENT,
    AttendanceStatus.IN_PROGRESS,
    AttendanceStatus.SHORT_HOURS,
}


@dataclass(slots=True)
class DayCounts:
    """The month in days. Fractions appear when half a day was on leave."""

    working_days: int = 0
    present_days: float = 0.0
    half_days: float = 0.0
    absent_days: float = 0.0
    paid_leave_days: float = 0.0
    unpaid_leave_days: float = 0.0
    missing_punch_out_days: float = 0.0
    holiday_days: float = 0.0
    #: Working days that have not happened yet — a projection assumes they will
    #: be worked, otherwise every month would read as a disaster on the 2nd.
    pending_days: float = 0.0
    #: Days the employee arrived after the grace period. Not a deduction on its
    #: own — the policy decides how many of them cost half a day.
    late_days: int = 0
    #: Working days, and calendar days, of the month before the employee joined.
    #: Both are kept because which one prorates the pay depends on the basis.
    before_joining_days: float = 0.0
    before_joining_calendar_days: int = 0
    #: Working days up to and including the go-live reset. Paid in full: people
    #: worked them, the system simply was not recording yet.
    untracked_days: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "working_days": self.working_days,
            "present_days": round(self.present_days, 2),
            "half_days": round(self.half_days, 2),
            "absent_days": round(self.absent_days, 2),
            "paid_leave_days": round(self.paid_leave_days, 2),
            "unpaid_leave_days": round(self.unpaid_leave_days, 2),
            "missing_punch_out_days": round(self.missing_punch_out_days, 2),
            "holiday_days": round(self.holiday_days, 2),
            "pending_days": round(self.pending_days, 2),
            "late_days": self.late_days,
            "before_joining_days": round(self.before_joining_days, 2),
            "before_joining_calendar_days": self.before_joining_calendar_days,
            "untracked_days": round(self.untracked_days, 2),
        }


@dataclass(slots=True)
class HourCounts:
    """The month in minutes, for somebody paid by the hours they put in.

    ``expected`` is every working day of the month at the shift's hours — the
    whole month, including days before joining, because the fixed amount is
    for a whole month. Everything else is what counts towards it.
    """

    expected_minutes: int = 0
    #: Punched on days that have already happened.
    worked_minutes: int = 0
    #: Paid leave, at the shift's hours for the part of the day taken.
    paid_leave_minutes: int = 0
    #: A day ruled a full day pays its full hours, however few were punched.
    full_day_minutes: int = 0
    #: Days still to come, assumed worked — as the day-based projection does.
    pending_minutes: int = 0
    #: Up to and including go-live: paid in full.
    untracked_minutes: int = 0

    @property
    def credited_minutes(self) -> int:
        return (
            self.worked_minutes
            + self.paid_leave_minutes
            + self.full_day_minutes
            + self.pending_minutes
            + self.untracked_minutes
        )

    @property
    def share(self) -> float:
        """The part of the fixed amount earned, never more than all of it."""
        if self.expected_minutes <= 0:
            return 0.0
        return min(1.0, self.credited_minutes / self.expected_minutes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_minutes": self.expected_minutes,
            "worked_minutes": self.worked_minutes,
            "paid_leave_minutes": self.paid_leave_minutes,
            "full_day_minutes": self.full_day_minutes,
            "pending_minutes": self.pending_minutes,
            "untracked_minutes": self.untracked_minutes,
            "credited_minutes": self.credited_minutes,
            "share": round(self.share, 4),
        }


@dataclass(slots=True)
class Projection:
    user_id: str
    month: str
    monthly_ctc: float
    effective_from: str | None
    day_rate: float
    day_basis: str
    counts: DayCounts
    deductions: list[dict[str, Any]] = field(default_factory=list)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    #: Set for somebody paid by the hour; None for the day-based majority.
    hours: HourCounts | None = None

    @property
    def total_deduction(self) -> float:
        return round(sum(line["amount"] for line in self.deductions), 2)

    @property
    def total_adjustment(self) -> float:
        return round(sum(line["amount"] for line in self.adjustments), 2)

    @property
    def projected_net(self) -> float:
        return round(self.monthly_ctc - self.total_deduction + self.total_adjustment, 2)


# --------------------------------------------------------------------------- #
# pure arithmetic
# --------------------------------------------------------------------------- #
def parse_month(month: str) -> tuple[int, int]:
    """``"2026-03"`` to ``(2026, 3)``."""
    try:
        year_part, month_part = month.split("-", 1)
        year, month_number = int(year_part), int(month_part)
        if not 1 <= month_number <= 12 or not 2000 <= year <= 2100:
            raise ValueError
    except ValueError as exc:
        raise ValidationError(
            "A month must look like '2026-03'.", code="INVALID_MONTH"
        ) from exc
    return year, month_number


def month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def salary_in_force(records: list[dict[str, Any]], *, on: str) -> dict[str, Any] | None:
    """The most recent record effective on or before ``on`` (``YYYY-MM-DD``)."""
    eligible = [record for record in records if record.get("effective_from", "") <= on]
    if not eligible:
        return None
    return max(eligible, key=lambda record: record["effective_from"])


def per_day_rate(
    monthly_ctc: float,
    basis: str,
    *,
    working_days: int,
    calendar_days: int,
) -> float:
    """What one day of this month is worth.

    A month with no working days at all divides by nothing rather than raising:
    there is no day to deduct, so the rate is irrelevant and zero is honest.
    """
    if basis == SalaryDayBasis.CALENDAR_DAYS:
        divisor = calendar_days
    elif basis == SalaryDayBasis.FIXED_26:
        divisor = 26
    elif basis == SalaryDayBasis.FIXED_30:
        divisor = 30
    else:
        divisor = working_days
    if divisor <= 0:
        return 0.0
    return round(monthly_ctc / divisor, 2)


def _late_penalty_days(late_days: int, flags: dict[str, Any]) -> float:
    """Every ``late_arrivals_per_half_day`` late arrivals cost half a day.

    Whole penalties only: with a threshold of three, two late arrivals cost
    nothing and the third completes the first half day. Charging a fraction of
    a penalty for each lateness would be a different policy, and a harsher one
    than the one an administrator thinks they switched on.
    """
    threshold = int(flags.get("late_arrivals_per_half_day", 0) or 0)
    if threshold <= 0 or late_days < threshold:
        return 0.0
    return (late_days // threshold) * 0.5


def _late_label(late_days: int, flags: dict[str, Any]) -> str:
    threshold = int(flags.get("late_arrivals_per_half_day", 0) or 0)
    return f"Late arrivals ({late_days} at {threshold} per half day)"


def deduction_lines(
    counts: DayCounts, *, day_rate: float, flags: dict[str, Any]
) -> list[dict[str, Any]]:
    """One line per rule the organisation has switched on. Days, then money.

    A half day costs half a day — the employee did work the other half.

    The month before somebody joined is not a policy and has no switch: it is
    the part of the month they were not employed for. It is counted in the
    unit the day rate was divided by, so a calendar-day basis prorates by
    calendar days and a working-day basis by working days.
    """
    calendar_basis = flags.get("salary_day_basis") in (
        SalaryDayBasis.CALENDAR_DAYS,
        SalaryDayBasis.FIXED_30,
    )
    candidates = [
        (
            "BEFORE_JOINING",
            "Before joining",
            counts.before_joining_calendar_days if calendar_basis else counts.before_joining_days,
            True,
        ),
        ("ABSENT", "Absent", counts.absent_days, flags.get("salary_deduct_absent", True)),
        (
            "UNPAID_LEAVE",
            "Unpaid leave",
            counts.unpaid_leave_days,
            flags.get("salary_deduct_unpaid_leave", True),
        ),
        (
            "HALF_DAY",
            "Half days",
            counts.half_days * 0.5,
            flags.get("salary_deduct_half_day", True),
        ),
        (
            "MISSING_PUNCH_OUT",
            "Missing punch-out",
            counts.missing_punch_out_days,
            flags.get("salary_deduct_missing_punch_out", False),
        ),
        (
            "LATE_ARRIVALS",
            _late_label(counts.late_days, flags),
            _late_penalty_days(counts.late_days, flags),
            True,
        ),
    ]
    return [
        {
            "kind": kind,
            "label": label,
            "days": round(days, 2),
            "amount": round(days * day_rate, 2),
        }
        for kind, label, days, enabled in candidates
        if enabled and days > 0
    ]


def _hours(minutes: int) -> float:
    return round(minutes / 60, 2)


def hours_lines(monthly_ctc: float, hours: HourCounts) -> list[dict[str, Any]]:
    """The one deduction an hourly-paid month has: the hours it fell short by.

    It replaces the day-based lines rather than adding to them. Absences, half
    days and late arrivals are already in the hours — charging them again would
    take the same time off twice.
    """
    shortfall = round(monthly_ctc * (1 - hours.share), 2)
    if shortfall <= 0:
        return []
    missing = max(0, hours.expected_minutes - hours.credited_minutes)
    return [
        {
            "kind": "HOURS_SHORT",
            "label": (
                f"Hours short ({_hours(hours.credited_minutes):g} of "
                f"{_hours(hours.expected_minutes):g} h)"
            ),
            "days": 0,
            "hours": _hours(missing),
            "amount": shortfall,
        }
    ]


def adjustment_lines(
    adjustments: list[dict[str, Any]], *, day_rate: float
) -> list[dict[str, Any]]:
    """Days into money. Positive pays back, negative takes away."""
    return [
        {
            "kind": adjustment.get("kind", SalaryAdjustmentKind.MANUAL),
            "days": round(float(adjustment.get("days", 0)), 2),
            "amount": round(float(adjustment.get("days", 0)) * day_rate, 2),
            "reason": adjustment.get("reason"),
            "source_month": adjustment.get("source_month"),
            "source_date": adjustment.get("source_date"),
            "reference_type": adjustment.get("reference_type"),
            "reference_id": adjustment.get("reference_id"),
        }
        for adjustment in adjustments
    ]


def count_day(
    counts: DayCounts,
    *,
    status: str | None,
    weight: float = 1.0,
) -> None:
    """Fold one judged working day into the running totals."""
    if status in _WORKED_STATUSES:
        counts.present_days += weight
    elif status == AttendanceStatus.HALF_DAY:
        counts.half_days += weight
    elif status == AttendanceStatus.MISSING_PUNCH_OUT:
        counts.missing_punch_out_days += weight
    else:
        counts.absent_days += weight


def build_projection(
    *,
    user_id: str,
    year: int,
    month: int,
    counts: DayCounts,
    salary: dict[str, Any] | None,
    org: dict[str, Any],
    adjustments: list[dict[str, Any]],
    hours: HourCounts | None = None,
) -> Projection:
    monthly_ctc = float((salary or {}).get("monthly_ctc", 0) or 0)
    basis = org.get("salary_day_basis", SalaryDayBasis.WORKING_DAYS)
    rate = per_day_rate(
        monthly_ctc,
        basis,
        working_days=counts.working_days,
        calendar_days=monthrange(year, month)[1],
    )
    return Projection(
        user_id=user_id,
        month=month_key(year, month),
        monthly_ctc=monthly_ctc,
        effective_from=(salary or {}).get("effective_from"),
        day_rate=rate,
        day_basis=str(basis),
        counts=counts,
        deductions=(
            hours_lines(monthly_ctc, hours)
            if hours is not None
            else deduction_lines(counts, day_rate=rate, flags=org)
        ),
        adjustments=adjustment_lines(adjustments, day_rate=rate),
        hours=hours,
    )


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _salaries():
    return get_collection(Collections.SALARIES)


def _adjustments():
    return get_collection(Collections.SALARY_ADJUSTMENTS)


def _attendance():
    return get_collection(Collections.ATTENDANCE)


def _leave_requests():
    return get_collection(Collections.LEAVE_REQUESTS)


def _leave_types():
    return get_collection(Collections.LEAVE_TYPES)


async def list_salaries(user_id: str) -> list[dict[str, Any]]:
    """The whole history, newest first. Nothing is ever overwritten."""
    await user_service.require_user(user_id)
    cursor = (
        _salaries()
        .find({"user_id": user_id, "is_deleted": False})
        .sort("effective_from", -1)
    )
    return [serialize(document) for document in await cursor.to_list(length=200)]


async def set_salary(user_id: str, payload: SalaryCreate, actor: Actor) -> dict[str, Any]:
    """Record a figure from a date. A second call on the same date corrects it."""
    await user_service.require_user(user_id)
    record = Salary(
        user_id=user_id,
        monthly_ctc=payload.monthly_ctc,
        effective_from=date_key(payload.effective_from),
        note=payload.note,
        set_by=actor.id,
        set_by_email=actor.email,
    )
    existing = await _salaries().find_one(
        {"user_id": user_id, "effective_from": record.effective_from}
    )
    document = record.to_mongo()
    document.pop("created_at", None)
    await _salaries().update_one(
        {"user_id": user_id, "effective_from": record.effective_from},
        {"$set": document, "$setOnInsert": {"created_at": record.created_at}},
        upsert=True,
    )
    stored = await _salaries().find_one(
        {"user_id": user_id, "effective_from": record.effective_from}
    )
    # Pay is the most sensitive thing an administrator can change, so who set
    # what, and when, is recorded before anything else can read it.
    await audit.record(
        actor,
        AuditAction.UPDATE if existing else AuditAction.CREATE,
        AuditEntity.SALARY,
        entity_id=user_id,
        entity_label=record.effective_from,
        before=serialize(existing) if existing else None,
        after=serialize(stored),
    )
    return serialize(stored)


async def record_adjustment(
    *,
    user_id: str,
    apply_month: str,
    days: float,
    reason: str,
    kind: SalaryAdjustmentKind = SalaryAdjustmentKind.MANUAL,
    source_month: str | None = None,
    source_date: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    actor: Actor | None = None,
) -> dict[str, Any]:
    """The integration point for a late approval. Days only — never an amount."""
    parse_month(apply_month)
    adjustment = SalaryAdjustment(
        user_id=user_id,
        apply_month=apply_month,
        source_month=source_month,
        source_date=source_date,
        kind=kind,
        days=days,
        reason=reason,
        reference_type=reference_type,
        reference_id=reference_id,
        created_by=actor.id if actor else None,
    )
    result = await _adjustments().insert_one(adjustment.to_mongo())
    stored = serialize(await _adjustments().find_one({"_id": result.inserted_id}))
    await audit.record(
        actor or audit.SYSTEM,
        AuditAction.CREATE,
        AuditEntity.SALARY,
        entity_id=user_id,
        entity_label=f"{kind} {days:+g} day(s) in {apply_month}",
        after=stored,
    )
    return stored


async def _adjustments_for(user_ids: list[str], month: str) -> dict[str, list[dict[str, Any]]]:
    cursor = _adjustments().find(
        {"user_id": {"$in": user_ids}, "apply_month": month, "is_deleted": False}
    )
    grouped: dict[str, list[dict[str, Any]]] = {user_id: [] for user_id in user_ids}
    for document in await cursor.to_list(length=1000):
        grouped.setdefault(document["user_id"], []).append(serialize(document))
    return grouped


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #
async def _leave_day_map(
    user_ids: list[str], start: date, end: date
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{user_id: {date: {"day_part", "is_paid"}}}`` for approved leave only.

    Whether a leave type is paid lives on the type, so the types are read once
    and joined here rather than trusting a snapshot on the request.
    """
    cursor = _leave_requests().find(
        {
            "user_id": {"$in": user_ids},
            "status": ApprovalStatus.APPROVED.value,
            "is_deleted": False,
            "days.date": {"$gte": date_key(start), "$lte": date_key(end)},
        }
    )
    requests = await cursor.to_list(length=2000)

    type_ids = {
        request.get("leave_type_id") for request in requests if request.get("leave_type_id")
    }
    paid_by_type: dict[str, bool] = {}
    if type_ids:
        types = await _leave_types().find(
            {"_id": {"$in": [ObjectId(value) for value in type_ids if ObjectId.is_valid(value)]}}
        ).to_list(length=100)
        paid_by_type = {str(item["_id"]): bool(item.get("is_paid", True)) for item in types}

    result: dict[str, dict[str, dict[str, Any]]] = {user_id: {} for user_id in user_ids}
    for request in requests:
        is_paid = paid_by_type.get(str(request.get("leave_type_id")), True)
        for day in request.get("days") or []:
            if not (date_key(start) <= day["date"] <= date_key(end)):
                continue
            result.setdefault(request["user_id"], {})[day["date"]] = {
                "day_part": day.get("day_part", "FULL"),
                "is_paid": is_paid,
            }
    return result


async def _attendance_map(
    user_ids: list[str], start: date, end: date
) -> dict[str, dict[str, dict[str, Any]]]:
    cursor = _attendance().find(
        {
            "user_id": {"$in": user_ids},
            "is_deleted": False,
            "date": {"$gte": date_key(start), "$lte": date_key(end)},
        }
    )
    result: dict[str, dict[str, dict[str, Any]]] = {user_id: {} for user_id in user_ids}
    for document in await cursor.to_list(length=5000):
        result.setdefault(document["user_id"], {})[document["date"]] = serialize(document)
    return result


def _count_month(
    decisions: list[calendar_service.DayDecision],
    *,
    today: date,
    attendance: dict[str, dict[str, Any]],
    leave: dict[str, dict[str, Any]],
    holidays: dict[str, Any],
    employed_from: date | None = None,
    tracking_from: date | None = None,
) -> DayCounts:
    """Turn a month of working days into the day buckets the deductions read.

    Only days that have already happened are judged. Today is left pending: an
    employee halfway through their shift has not been absent.

    Half a day on leave leaves half a day to judge, hence the weight. It cannot
    double-count against the attendance status: a half-day leave already halves
    what the shift expects, so working the other half reads as a full present
    day rather than a half day.
    """
    counts = DayCounts()
    for decision in decisions:
        day_key = decision.date
        # The attendance rules' safeguard, mirrored: a day with a record on it
        # is judged on that record, even when the joining date says otherwise.
        not_yet_joined = (
            employed_from is not None
            and decision.day < employed_from
            and day_key not in attendance
        )
        if not_yet_joined:
            counts.before_joining_calendar_days += 1

        # A declared holiday closes the day in the work calendar, so it is not a
        # working day and must not sit in the denominator of the per-day rate.
        # It is still reported, because "paid, nobody was expected in" is a
        # different line from "the office is never open on a Sunday".
        if decision.source == calendar_service.SOURCE_HOLIDAY or (
            day_key in holidays and not decision.is_working
        ):
            counts.holiday_days += 1
            continue

        if not decision.is_working:
            continue
        counts.working_days += 1

        # Ahead of the pending check: a joiner later this month has not "yet
        # to work" the days before their start, they will never work them.
        if not_yet_joined:
            counts.before_joining_days += 1
            continue

        # Up to and including go-live the day is paid as worked: nothing was
        # recorded that could fairly cost anybody anything.
        if tracking_from is not None and decision.day <= tracking_from:
            counts.untracked_days += 1
            continue

        if decision.day >= today:
            counts.pending_days += 1
            continue

        leave_day = leave.get(day_key)
        weight = 1.0
        if leave_day:
            taken = 1.0 if leave_day["day_part"] == "FULL" else 0.5
            if leave_day["is_paid"]:
                counts.paid_leave_days += taken
            else:
                counts.unpaid_leave_days += taken
            weight = 1.0 - taken
            if weight <= 0:
                continue

        stored = attendance.get(day_key) or {}
        if stored.get("is_late"):
            counts.late_days += 1
        count_day(counts, status=stored.get("status"), weight=weight)

    return counts


def _count_hours(
    decisions: list[calendar_service.DayDecision],
    *,
    today: date,
    attendance: dict[str, dict[str, Any]],
    leave: dict[str, dict[str, Any]],
    holidays: dict[str, Any],
    shift: dict[str, Any] | None,
    full_days: dict[str, Any],
    employed_from: date | None = None,
    tracking_from: date | None = None,
) -> HourCounts:
    """What an hourly-paid month asked for, and what counts towards it.

    Hours are pooled across the month: a long Tuesday makes up for a short
    Monday, because the fixed amount is for the month, not for each day.
    """
    hours = HourCounts()
    for decision in decisions:
        day_key = decision.date
        if decision.source == calendar_service.SOURCE_HOLIDAY or (
            day_key in holidays and not decision.is_working
        ):
            continue
        if not decision.is_working:
            continue

        stored = attendance.get(day_key) or {}
        day_shift = stored.get("shift_snapshot") or shift or {}
        required = int(day_shift.get("required_minutes") or DEFAULT_REQUIRED_MINUTES)
        hours.expected_minutes += required

        # Before joining: part of the month, earning nothing. A day with a
        # record on it is judged on the record, as the day count does.
        if employed_from is not None and decision.day < employed_from and not stored:
            continue
        if tracking_from is not None and decision.day <= tracking_from:
            hours.untracked_minutes += required
            continue
        if decision.day >= today:
            hours.pending_minutes += required
            continue

        weight = 1.0
        leave_day = leave.get(day_key)
        if leave_day:
            taken = 1.0 if leave_day["day_part"] == "FULL" else 0.5
            if leave_day["is_paid"]:
                hours.paid_leave_minutes += round(required * taken)
            weight = 1.0 - taken

        worked = int(stored.get("worked_minutes") or 0)
        hours.worked_minutes += worked
        if day_key in full_days and stored.get("sessions") and weight > 0:
            hours.full_day_minutes += max(0, round(required * weight) - worked)
    return hours


async def project_month(
    user_ids: list[str], month: str, *, today: date | None = None
) -> list[dict[str, Any]]:
    """A month's figures for one or many employees.

    Shifts are resolved per user but the Saturday pattern, the overrides and the
    holidays are read once for the whole organisation — an org-wide projection
    must not turn into one round trip per employee per day.
    """
    year, month_number = parse_month(month)
    first, last = month_range(year, month_number)
    today = today or org_today()

    org = await settings_service.get_settings()
    overrides = await calendar_service.override_map(first, last)
    holidays = await holiday_service.holiday_map(first, last)
    # Read through the work calendar rather than off the settings document:
    # two places defaulting the pattern is two places to get it wrong, and a
    # projection that disagrees with the calendar is worse than either.
    working_saturdays, fifth = await calendar_service.saturday_pattern()
    tracking_from = await settings_service.tracking_start()
    full_days = await calendar_service.full_day_map(first, last)

    attendance = await _attendance_map(user_ids, first, last)
    leave = await _leave_day_map(user_ids, first, last)
    adjustments = await _adjustments_for(user_ids, month)

    decisions_by_shift: dict[str, list[calendar_service.DayDecision]] = {}
    projections: list[dict[str, Any]] = []

    for user_id in user_ids:
        user = await user_service.require_user(user_id)
        shift = await shift_service.resolve_for_user(user)
        cache_key = str((shift or {}).get("id"))
        if cache_key not in decisions_by_shift:
            decisions_by_shift[cache_key] = calendar_service.decide_range(
                first,
                last,
                shift=shift,
                working_saturdays=working_saturdays,
                fifth_saturday_working=fifth,
                overrides=overrides,
                holidays=holidays,
            )

        counts = _count_month(
            decisions_by_shift[cache_key],
            today=today,
            attendance=attendance.get(user_id, {}),
            leave=leave.get(user_id, {}),
            holidays=holidays,
            employed_from=employment_start(user),
            tracking_from=tracking_from,
        )

        hours = None
        if user.get("employment_type") == EmploymentType.PART_TIME.value:
            hours = _count_hours(
                decisions_by_shift[cache_key],
                today=today,
                attendance=attendance.get(user_id, {}),
                leave=leave.get(user_id, {}),
                holidays=holidays,
                shift=shift,
                full_days=full_days,
                employed_from=employment_start(user),
                tracking_from=tracking_from,
            )

        history = await list_salaries(user_id)
        salary = salary_in_force(history, on=date_key(last))
        projection = build_projection(
            user_id=user_id,
            year=year,
            month=month_number,
            counts=counts,
            salary=salary,
            org=org,
            adjustments=adjustments.get(user_id, []),
            hours=hours,
        )
        projections.append(
            {
                "user_id": user_id,
                "user_name": " ".join(
                    part for part in (user.get("first_name"), user.get("last_name")) if part
                ),
                "user_email": user.get("email"),
                "employment_type": user.get("employment_type", EmploymentType.FULL_TIME.value),
                "has_salary_on_record": salary is not None,
                **_serialise(projection),
            }
        )

    return projections


def _serialise(projection: Projection) -> dict[str, Any]:
    return {
        "month": projection.month,
        "monthly_ctc": projection.monthly_ctc,
        "effective_from": projection.effective_from,
        "day_basis": projection.day_basis,
        "day_rate": projection.day_rate,
        "pay_basis": "HOURS" if projection.hours is not None else "DAYS",
        "hours": projection.hours.as_dict() if projection.hours is not None else None,
        "counts": projection.counts.as_dict(),
        "deductions": projection.deductions,
        "adjustments": projection.adjustments,
        "total_deduction": projection.total_deduction,
        "total_adjustment": projection.total_adjustment,
        "projected_net": projection.projected_net,
    }


async def project_for_user(
    user_id: str, month: str, *, today: date | None = None
) -> dict[str, Any]:
    await user_service.require_user(user_id)
    return (await project_month([user_id], month, today=today))[0]


async def project_organisation(month: str, *, today: date | None = None) -> dict[str, Any]:
    """Every active account. Accounts without a salary on record are listed too,
    with a zero figure — a missing salary is something an admin needs to see."""
    users = await _active_user_ids()
    projections = await project_month(users, month, today=today)
    return {
        "month": month,
        "employees": projections,
        "total_projected_net": round(
            sum(item["projected_net"] for item in projections), 2
        ),
        "missing_salary_count": sum(
            1 for item in projections if not item["has_salary_on_record"]
        ),
    }


async def _active_user_ids() -> list[str]:
    cursor = (
        get_collection(Collections.USERS)
        .find({"is_deleted": False, "status": UserStatus.ACTIVE.value}, {"_id": 1})
        .sort("_id", 1)
    )
    return [str(document["_id"]) for document in await cursor.to_list(length=1000)]
