"""Working days the shift pattern alone cannot express — Saturdays, swaps, comp-offs.

Reading the calendar is open to every employee: knowing whether this Saturday is
working is not privileged information, and the answer depends on the reader's own
shift. Changing it is an administrator's job.
"""

from datetime import date

from fastapi import APIRouter, Query, status

from app.schemas.comp_off import CompOffBalanceOut, CompOffOut
from app.schemas.work_calendar import (
    DayOut,
    MonthCalendarOut,
    OverrideCreate,
    OverrideOut,
    SwapCreate,
)
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
    return success(
        MonthCalendarOut(
            year=year,
            month=month,
            working_days=calendar_service.count_working_days(decisions),
            days=[DayOut.from_decision(decision) for decision in decisions],
        )
    )


@router.get("/day", summary="What one date is, and which rule decided it")
async def day(current_user: CurrentUser, on: date) -> dict:
    decision = await calendar_service.decide(on, user=current_user)
    return success(DayOut.from_decision(decision))


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
