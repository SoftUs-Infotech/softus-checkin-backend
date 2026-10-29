"""Which days the office is open.

The top half of this module is pure: given a shift, the organisation's Saturday
pattern and at most one override document, it decides what a date is. No
database, so every branch is unit-testable without a server. The bottom half
does the lookups and the writing.

Precedence, highest first:

1. A per-date override — an admin has ruled on this exact day, including both
   halves of a swap. It beats everything, so a holiday can still be opened by
   hand for the one year the office decides to work it.
2. A declared holiday closes the day. Holidays are not a separate calendar that
   the rest of the system has to remember to consult: a holiday is simply a day
   the office is shut, so it lands here and every working-day count, every
   salary denominator and every employee's calendar follows from the one rule.
3. Saturday follows the organisation's nth-Saturday pattern, because that is a
   company-wide policy rather than a property of anybody's shift. A shift that
   lists Saturday among its working weekdays opts out: it has said explicitly
   that these people work every Saturday.
4. Every other weekday is decided by the shift's ``working_weekdays``.

The fifth Saturday is deliberately tri-state. Most organisations decide it late
in the month, and guessing on their behalf is how somebody ends up unpaid for a
day they were told to work. Undecided reads as off and says so, so the UI can
ask rather than pretend.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from bson import ObjectId

from app.core.datetime import date_key, iter_dates, month_range, utc_now
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.settings import OrgSettings
from app.models.work_calendar import (
    DayKind,
    FullDayCredit,
    OverrideSource,
    WorkCalendarOverride,
)
from app.schemas.work_calendar import FullDayCreate, OverrideCreate, SwapCreate
from app.services import audit
from app.services import comp_off as comp_off_service
from app.services import holiday as holiday_service
from app.services import settings as settings_service
from app.services import shift as shift_service
from app.services.audit import Actor

SATURDAY = 5

DEFAULT_WORKING_WEEKDAYS = (0, 1, 2, 3, 4)

#: Where a ruling came from, for the UI and for explaining a day to a human.
SOURCE_OVERRIDE = "OVERRIDE"
SOURCE_SWAP = "SWAP"
SOURCE_HOLIDAY = "HOLIDAY"
SOURCE_SATURDAY_PATTERN = "SATURDAY_PATTERN"
SOURCE_SHIFT = "SHIFT"


@dataclass(frozen=True, slots=True)
class DayDecision:
    day: date
    is_working: bool
    source: str
    reason: str
    #: True only for a fifth Saturday nobody has ruled on yet.
    is_undecided: bool = False
    swap_with_date: str | None = None
    #: Set whenever a declared holiday falls on this date, even if an override
    #: has opened the office anyway — the reader still wants to know why.
    holiday_name: str | None = None

    @property
    def date(self) -> str:
        return date_key(self.day)


# --------------------------------------------------------------------------- #
# pure rules
# --------------------------------------------------------------------------- #
def nth_weekday_of_month(day: date) -> int:
    """1 for the first Saturday of the month, 2 for the second, and so on."""
    return (day.day - 1) // 7 + 1


def shift_working_weekdays(shift: dict[str, Any] | None) -> set[int]:
    weekdays = (shift or {}).get("working_weekdays")
    return set(weekdays or DEFAULT_WORKING_WEEKDAYS)


def saturday_is_working(
    day: date,
    *,
    working_saturdays: list[int] | None,
    fifth_saturday_working: bool | None,
) -> tuple[bool, bool]:
    """``(is_working, is_undecided)`` for a Saturday under the org pattern."""
    ordinal = nth_weekday_of_month(day)
    pattern = set(working_saturdays or [])

    if ordinal >= 5:
        if fifth_saturday_working is not None:
            return fifth_saturday_working, False
        # The list may still carry an explicit answer; only silence is undecided.
        if 5 in pattern:
            return True, False
        return False, True

    return ordinal in pattern, False


def decide_working_day(
    day: date,
    *,
    shift: dict[str, Any] | None = None,
    working_saturdays: list[int] | None = None,
    fifth_saturday_working: bool | None = None,
    override: dict[str, Any] | None = None,
    holiday: dict[str, Any] | None = None,
) -> DayDecision:
    """What a single date is. See the module docstring for precedence."""
    holiday_name = (holiday or {}).get("name")

    if override:
        kind = override.get("kind")
        swap_with = override.get("swap_with_date")
        return DayDecision(
            day=day,
            is_working=kind == DayKind.WORKING,
            source=SOURCE_SWAP if swap_with else SOURCE_OVERRIDE,
            reason=override.get("reason") or "Set by an administrator.",
            swap_with_date=swap_with,
            holiday_name=holiday_name,
        )

    if holiday:
        return DayDecision(
            day=day,
            is_working=False,
            source=SOURCE_HOLIDAY,
            reason=f"{holiday_name} — the office is closed.",
            holiday_name=holiday_name,
        )

    weekdays = shift_working_weekdays(shift)

    # The Saturday pattern is organisation policy and always decides a Saturday.
    # It used to defer to a shift that listed Saturday among its working days,
    # which silently killed the setting: almost every shift lists Saturday, so
    # "the 2nd and 4th are working" never took effect for anybody. Only a
    # per-date ruling, handled above, beats the pattern.
    if day.weekday() == SATURDAY:
        working, undecided = saturday_is_working(
            day,
            working_saturdays=working_saturdays,
            fifth_saturday_working=fifth_saturday_working,
        )
        ordinal = nth_weekday_of_month(day)
        if undecided:
            reason = "The fifth Saturday has not been decided; treated as off."
        else:
            reason = f"Saturday {ordinal} of the month is {'working' if working else 'off'}."
        return DayDecision(
            day=day,
            is_working=working,
            source=SOURCE_SATURDAY_PATTERN,
            reason=reason,
            is_undecided=undecided,
        )

    working = day.weekday() in weekdays
    return DayDecision(
        day=day,
        is_working=working,
        source=SOURCE_SHIFT,
        reason="The shift covers this weekday." if working else "Weekly off.",
    )


def decide_range(
    start: date,
    end: date,
    *,
    shift: dict[str, Any] | None = None,
    working_saturdays: list[int] | None = None,
    fifth_saturday_working: bool | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    holidays: dict[str, dict[str, Any]] | None = None,
) -> list[DayDecision]:
    """Every date in ``[start, end]``, decided. Pure — hand it the overrides."""
    overrides = overrides or {}
    holidays = holidays or {}
    return [
        decide_working_day(
            day,
            shift=shift,
            working_saturdays=working_saturdays,
            fifth_saturday_working=fifth_saturday_working,
            override=overrides.get(date_key(day)),
            holiday=holidays.get(date_key(day)),
        )
        for day in iter_dates(start, end)
    ]


def count_working_days(decisions: list[DayDecision]) -> int:
    return sum(1 for decision in decisions if decision.is_working)


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #
def _calendar():
    return get_collection(Collections.WORK_CALENDAR)


async def saturday_pattern() -> tuple[list[int], bool | None]:
    """The Saturday pattern, defaulting the way the settings model does.

    A settings document written before the field existed has no value, and
    reading that as "no Saturday is working" is a different policy from the one
    the organisation was told it had. An administrator who genuinely wants every
    Saturday closed stores an empty list, which is respected.
    """
    org = await settings_service.get_settings()
    stored = org.get("working_saturdays")
    saturdays = OrgSettings.model_fields["working_saturdays"].get_default(
        call_default_factory=True
    )
    return (list(stored) if isinstance(stored, list) else saturdays), org.get(
        "fifth_saturday_working"
    )


#: The old private name, kept so callers inside this module read the same way.
_pattern = saturday_pattern


async def override_map(start: date, end: date) -> dict[str, dict[str, Any]]:
    """``{date_key: override}`` for a range."""
    cursor = _calendar().find(
        {
            "is_deleted": False,
            "date": {"$gte": date_key(start), "$lte": date_key(end)},
        }
    )
    return {
        document["date"]: serialize(document)
        for document in await cursor.to_list(length=400)
    }


async def get_override(day: date) -> dict[str, Any] | None:
    document = await _calendar().find_one({"date": date_key(day), "is_deleted": False})
    return serialize(document)


async def list_overrides(start: date, end: date) -> list[dict[str, Any]]:
    cursor = (
        _calendar()
        .find(
            {
                "is_deleted": False,
                "date": {"$gte": date_key(start), "$lte": date_key(end)},
            }
        )
        .sort("date", 1)
    )
    return [serialize(document) for document in await cursor.to_list(length=400)]


async def _shift_for(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if user is None:
        return await shift_service.get_default_shift()
    return await shift_service.resolve_for_user(user)


async def decide(day: date, *, user: dict[str, Any] | None = None) -> DayDecision:
    """The one call the attendance engine needs: is this date worked?"""
    working_saturdays, fifth = await _pattern()
    holidays = await holiday_service.holiday_map(day, day)
    return decide_working_day(
        day,
        shift=await _shift_for(user),
        working_saturdays=working_saturdays,
        fifth_saturday_working=fifth,
        override=await get_override(day),
        holiday=holidays.get(date_key(day)),
    )


async def is_working_day(day: date, *, user: dict[str, Any] | None = None) -> bool:
    return (await decide(day, user=user)).is_working


async def decisions_for_range(
    start: date, end: date, *, user: dict[str, Any] | None = None
) -> list[DayDecision]:
    working_saturdays, fifth = await _pattern()
    return decide_range(
        start,
        end,
        shift=await _shift_for(user),
        working_saturdays=working_saturdays,
        fifth_saturday_working=fifth,
        overrides=await override_map(start, end),
        holidays=await holiday_service.holiday_map(start, end),
    )


async def month_decisions(
    year: int, month: int, *, user: dict[str, Any] | None = None
) -> list[DayDecision]:
    first, last = month_range(year, month)
    return await decisions_for_range(first, last, user=user)


async def working_days_in_month(
    year: int, month: int, *, user: dict[str, Any] | None = None
) -> int:
    """How many days the month asks for — the denominator of the per-day rate."""
    return count_working_days(await month_decisions(year, month, user=user))


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
async def _write_override(
    day: date,
    kind: DayKind,
    reason: str,
    actor: Actor,
    *,
    source: OverrideSource = OverrideSource.MANUAL,
    swap_with_date: str | None = None,
) -> dict[str, Any]:
    """Upsert one ruling. The unique index on ``date`` keeps it to one per day."""
    override = WorkCalendarOverride(
        date=date_key(day),
        kind=kind,
        reason=reason,
        source=source,
        swap_with_date=swap_with_date,
        set_by=actor.id,
        set_by_email=actor.email,
    )
    document = override.to_mongo()
    document.pop("created_at", None)
    await _calendar().update_one(
        {"date": date_key(day)},
        {"$set": document, "$setOnInsert": {"created_at": utc_now()}},
        upsert=True,
    )
    return await get_override(day)


async def _credit_if_taken_away(
    before: DayDecision, after: DayDecision, reason: str, actor: Actor
) -> int:
    """A day that was working and is now off owes a comp-off to whoever worked it."""
    if not before.is_working or after.is_working:
        return 0
    return await comp_off_service.credit_for_date(before.day, reason=reason, actor=actor)


async def set_override(payload: OverrideCreate, actor: Actor) -> dict[str, Any]:
    existing = await get_override(payload.date)
    if existing and existing.get("source") == OverrideSource.SWAP:
        raise ConflictError(
            f"{date_key(payload.date)} is one half of a swap with "
            f"{existing.get('swap_with_date')}. Remove the swap before overriding it.",
            code="OVERRIDE_IS_SWAPPED",
        )

    before = await decide(payload.date)
    override = await _write_override(payload.date, payload.kind, payload.reason, actor)
    after = await decide(payload.date)

    credited = await _credit_if_taken_away(before, after, payload.reason, actor)
    # A ruling moves a working day, and a working day is worth money once the
    # per-day rate is computed from it. Who decided is part of the record.
    await audit.record(
        actor,
        AuditAction.UPDATE if existing else AuditAction.CREATE,
        AuditEntity.WORK_CALENDAR,
        entity_id=override.get("id"),
        entity_label=f"{date_key(payload.date)} {payload.kind}",
        before=existing,
        after=override,
    )
    return {"override": override, "comp_offs_credited": credited}


async def remove_override(day: date, actor: Actor) -> None:
    existing = await get_override(day)
    if existing is None:
        raise NotFoundError(
            f"No ruling is recorded for {date_key(day)}.", code="OVERRIDE_NOT_FOUND"
        )
    if existing.get("source") == OverrideSource.SWAP:
        raise ConflictError(
            f"{date_key(day)} is one half of a swap with {existing.get('swap_with_date')}. "
            "Remove the swap instead, so the month keeps its working-day count.",
            code="OVERRIDE_IS_SWAPPED",
        )
    await _calendar().delete_one({"_id": ObjectId(existing["id"])})
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.WORK_CALENDAR,
        entity_id=existing["id"],
        entity_label=date_key(day),
        before=existing,
    )


# --------------------------------------------------------------------------- #
# days counted as a full day
# --------------------------------------------------------------------------- #
def _full_days():
    return get_collection(Collections.FULL_DAY_CREDITS)


async def full_day_map(start: date, end: date) -> dict[str, dict[str, Any]]:
    """``{date_key: ruling}`` for the dates in a range counted as a full day."""
    cursor = _full_days().find({"date": {"$gte": date_key(start), "$lte": date_key(end)}})
    return {
        document["date"]: serialize(document)
        for document in await cursor.to_list(length=400)
    }


async def is_full_day(day: date) -> bool:
    return await _full_days().count_documents({"date": date_key(day)}, limit=1) > 0


async def set_full_day(payload: FullDayCreate, actor: Actor) -> dict[str, Any]:
    """Count a date as a full day for everybody who punched in.

    Re-marking an already marked date only updates the reason. Re-judging the
    days already recorded is the caller's job, so this module stays free of the
    attendance engine that depends on it.
    """
    day_key = date_key(payload.date)
    existing = serialize(await _full_days().find_one({"date": day_key}))
    ruling = FullDayCredit(
        date=day_key,
        reason=payload.reason.strip(),
        set_by=actor.id,
        set_by_email=actor.email,
    ).to_mongo()
    ruling.pop("created_at", None)
    await _full_days().update_one(
        {"date": day_key},
        {"$set": ruling, "$setOnInsert": {"created_at": utc_now()}},
        upsert=True,
    )
    saved = serialize(await _full_days().find_one({"date": day_key}))
    # It changes what people are paid for the day, so who decided is on record.
    await audit.record(
        actor,
        AuditAction.UPDATE if existing else AuditAction.CREATE,
        AuditEntity.WORK_CALENDAR,
        entity_id=saved["id"],
        entity_label=f"{day_key} counted as a full day",
        before=existing,
        after=saved,
    )
    return saved


async def clear_full_day(day: date, actor: Actor) -> None:
    day_key = date_key(day)
    existing = serialize(await _full_days().find_one({"date": day_key}))
    if existing is None:
        raise NotFoundError(
            f"{day_key} is not counted as a full day.", code="FULL_DAY_NOT_FOUND"
        )
    await _full_days().delete_one({"date": day_key})
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.WORK_CALENDAR,
        entity_id=existing["id"],
        entity_label=f"{day_key} no longer counted as a full day",
        before=existing,
    )


def _months_touched(*days: date) -> list[tuple[int, int]]:
    return sorted({(day.year, day.month) for day in days})


async def _count_with(
    year: int, month: int, extra: dict[str, dict[str, Any]]
) -> tuple[int, int]:
    """``(before, after)`` working days for a month, given proposed rulings."""
    first, last = month_range(year, month)
    stored = await override_map(first, last)
    shift = await _shift_for(None)
    saturdays, fifth = await _pattern()
    holidays = await holiday_service.holiday_map(first, last)

    def count(overrides: dict[str, dict[str, Any]]) -> int:
        return count_working_days(
            decide_range(
                first,
                last,
                shift=shift,
                working_saturdays=saturdays,
                fifth_saturday_working=fifth,
                overrides=overrides,
                holidays=holidays,
            )
        )

    return count(stored), count({**stored, **extra})


async def create_swap(payload: SwapCreate, actor: Actor) -> dict[str, Any]:
    """Take one working day off and open one that was off, keeping the count.

    The count is checked rather than assumed, and it is checked *before* anything
    is written: a swap whose two halves land in different months, or whose dates
    are not already one working and one off, would quietly change a month's
    per-day rate — and therefore everybody's pay.
    """
    if payload.off_date == payload.working_date:
        raise ValidationError(
            "A swap needs two different dates.", code="SWAP_SAME_DATE"
        )

    for day in (payload.off_date, payload.working_date):
        existing = await get_override(day)
        if existing and existing.get("source") == OverrideSource.SWAP:
            raise ConflictError(
                f"{date_key(day)} is already swapped with {existing.get('swap_with_date')}.",
                code="ALREADY_SWAPPED",
            )

    proposed = {
        date_key(payload.off_date): {
            "kind": DayKind.OFF,
            "reason": payload.reason,
            "swap_with_date": date_key(payload.working_date),
        },
        date_key(payload.working_date): {
            "kind": DayKind.WORKING,
            "reason": payload.reason,
            "swap_with_date": date_key(payload.off_date),
        },
    }

    months = _months_touched(payload.off_date, payload.working_date)
    counts = {month: await _count_with(*month, proposed) for month in months}
    changed = [month for month in months if counts[month][0] != counts[month][1]]
    if changed:
        month_labels = ", ".join(f"{year}-{month:02d}" for year, month in changed)
        raise ConflictError(
            "That swap would change the number of working days in "
            f"{month_labels}. A swap must take one working day off and open one "
            "day that was off, in the same month.",
            code="SWAP_CHANGES_WORKING_DAYS",
        )

    off_before = await decide(payload.off_date)

    await _write_override(
        payload.off_date,
        DayKind.OFF,
        payload.reason,
        actor,
        source=OverrideSource.SWAP,
        swap_with_date=date_key(payload.working_date),
    )
    await _write_override(
        payload.working_date,
        DayKind.WORKING,
        payload.reason,
        actor,
        source=OverrideSource.SWAP,
        swap_with_date=date_key(payload.off_date),
    )

    off_after = await decide(payload.off_date)
    credited = await _credit_if_taken_away(off_before, off_after, payload.reason, actor)

    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.WORK_CALENDAR,
        entity_label=(
            f"{date_key(payload.off_date)} off in place of {date_key(payload.working_date)}"
        ),
        after={
            "off_date": date_key(payload.off_date),
            "working_date": date_key(payload.working_date),
            "reason": payload.reason,
        },
    )

    return {
        "off_date": date_key(payload.off_date),
        "working_date": date_key(payload.working_date),
        "reason": payload.reason,
        "comp_offs_credited": credited,
        "working_days": {
            f"{year}-{month:02d}": counts[(year, month)][1] for year, month in months
        },
    }


async def remove_swap(day: date, actor: Actor) -> dict[str, Any]:
    existing = await get_override(day)
    if existing is None or existing.get("source") != OverrideSource.SWAP:
        raise NotFoundError(
            f"No swap is recorded for {date_key(day)}.", code="SWAP_NOT_FOUND"
        )
    partner = existing.get("swap_with_date")
    removed = [existing["date"], partner] if partner else [existing["date"]]
    await _calendar().delete_many({"date": {"$in": removed}})
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.WORK_CALENDAR,
        entity_label=f"swap {' / '.join(removed)}",
        before=existing,
    )
    return {"removed": removed}
