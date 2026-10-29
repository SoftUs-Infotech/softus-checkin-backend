"""Working days the shift pattern alone cannot express — Saturdays, swaps, comp-offs.

Reading the calendar is open to every employee: knowing whether this Saturday is
working is not privileged information, and the answer depends on the reader's own
shift. Changing it is an administrator's job.
"""

from datetime import date

from fastapi import APIRouter, Query, status

from app.core.datetime import month_range
from app.schemas.comp_off import CompOffBalanceOut, CompOffOut
from app.schemas.work_calendar import (
    DayOut,
    FullDayCreate,
    FullDayOut,
    MonthCalendarOut,
    OverrideCreate,
    OverrideOut,
    SwapCreate,
)
from app.services import attendance as attendance_service
from app.services import comp_off as comp_off_service
from app.services import work_calendar as calendar_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/work-calendar", tags=["work calendar"])


@router.get("/month", summary="A month of working days, for the caller's shift")
async def month_calendar(
    current_user: CurrentUser,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
) -> dict:
    decisions = await calendar_service.month_decisions(year, month, user=current_user)
    full_days = await calendar_service.full_day_map(*month_range(year, month))
    return success(
        MonthCalendarOut(
            year=year,
            month=month,
            working_days=calendar_service.count_working_days(decisions),
            days=[
                DayOut.from_decision(decision, full_days.get(decision.date))
                for decision in decisions
            ],
        )
    )


@router.get("/day", summary="What one date is, and which rule decided it")
async def day(current_user: CurrentUser, on: date) -> dict:
    decision = await calendar_service.decide(on, user=current_user)
    full_days = await calendar_service.full_day_map(on, on)
    return success(DayOut.from_decision(decision, full_days.get(decision.date)))


@router.get("/overrides", summary="Per-date rulings in a range")
async def list_overrides(_: CurrentUser, from_date: date, to_date: date) -> dict:
    overrides = await calendar_service.list_overrides(from_date, to_date)
    return success([OverrideOut.model_validate(item) for item in overrides])


@router.post(
    "/overrides", status_code=status.HTTP_201_CREATED, summary="Rule on a single date"
)
async def set_override(payload: OverrideCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    result = await calendar_service.set_override(payload, actor)
    message = "Calendar updated."
    if result["comp_offs_credited"]:
        message += f" {result['comp_offs_credited']} comp-off(s) credited."
    return success(
        {
            "override": OverrideOut.model_validate(result["override"]),
            "comp_offs_credited": result["comp_offs_credited"],
        },
        message=message,
    )


@router.delete("/overrides/{on}", summary="Drop a per-date ruling")
async def remove_override(on: date, _: CurrentAdmin, actor: CurrentActor) -> dict:
    await calendar_service.remove_override(on, actor)
    return success(message="Ruling removed.")


@router.get("/full-days", summary="Dates counted as a full day for everybody who came in")
async def list_full_days(_: CurrentUser, from_date: date, to_date: date) -> dict:
    rulings = await calendar_service.full_day_map(from_date, to_date)
    return success(
        [FullDayOut.model_validate(item) for _, item in sorted(rulings.items())]
    )


@router.post("/full-days", summary="Count a date as a full day for everybody who came in")
async def set_full_day(payload: FullDayCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    """For the day the office opens for a few hours on purpose.

    Everybody who punched in that date is present for a full day — no half
    day, no short hours, no late mark — whenever this is set, before they
    arrive or after they have all gone home. Leave and absence are untouched.
    """
    ruling = await calendar_service.set_full_day(payload, actor)
    updated = await attendance_service.recompute_everyone_on(payload.date)
    return success(
        FullDayOut.model_validate(ruling),
        message=(
            f"{ruling['date']} counts as a full day for everybody who came in. "
            f"{updated} attendance record(s) updated."
        ),
    )


@router.delete("/full-days/{on}", summary="Judge a date by its hours again")
async def clear_full_day(on: date, _: CurrentAdmin, actor: CurrentActor) -> dict:
    await calendar_service.clear_full_day(on, actor)
    updated = await attendance_service.recompute_everyone_on(on)
    return success(
        {"updated": updated},
        message=f"{on.isoformat()} is judged by hours worked again.",
    )


@router.post(
    "/swaps",
    status_code=status.HTTP_201_CREATED,
    summary="Take one working day off and open another",
)
async def create_swap(payload: SwapCreate, _: CurrentAdmin, actor: CurrentActor) -> dict:
    result = await calendar_service.create_swap(payload, actor)
    message = f"{result['off_date']} swapped with {result['working_date']}."
    if result["comp_offs_credited"]:
        message += f" {result['comp_offs_credited']} comp-off(s) credited."
    return success(result, message=message)


@router.delete("/swaps/{on}", summary="Undo a swap, both halves at once")
async def remove_swap(on: date, _: CurrentAdmin, actor: CurrentActor) -> dict:
    result = await calendar_service.remove_swap(on, actor)
    return success(result, message="Swap removed.")


@router.get("/comp-offs", summary="Comp-off ledger")
async def list_comp_offs(
    current_user: CurrentUser,
    user_id: str | None = None,
    available_only: bool = False,
    scope: str = Query(
        "self",
        pattern="^(self|org)$",
        description="'org' is the whole organisation's ledger, for administrators.",
    ),
) -> dict:
    if scope == "org":
        comp_off_service.require_org_ledger_access(current_user)
        credits = await comp_off_service.list_for_org(include_spent=not available_only)
    else:
        target = comp_off_service.target_user_id(current_user, user_id)
        credits = await comp_off_service.list_for_user(target, include_spent=not available_only)
    return success([CompOffOut.model_validate(item) for item in credits])


@router.get("/comp-offs/balance", summary="Comp-off balance")
async def comp_off_balance(current_user: CurrentUser, user_id: str | None = None) -> dict:
    target = comp_off_service.target_user_id(current_user, user_id)
    return success(CompOffBalanceOut.model_validate(await comp_off_service.balance(target)))
