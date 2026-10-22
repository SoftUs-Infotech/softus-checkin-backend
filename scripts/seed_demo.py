"""Fill the database with a realistic demo organisation.

    python scripts/seed_demo.py            # add demo data, keep what exists
    python scripts/seed_demo.py --reset    # wipe the database first

Creates an administrator and four employees with known passwords, then back-fills
six weeks of attendance so every calendar colour actually appears: full days,
a half day, a short day, a forgotten punch-out, an absence, approved leave and a
holiday. It also leaves work in the approval queues so the admin screens are not
empty — enough of it that the bulk selection has something to act on.

Every screen built since should have something to show. Pay is on record for all
but one employee, so the "no salary set" warning appears; the current month has a
Saturday swap and a day closed by hand, with the comp-offs those credit; tasks
sit at every stage of progress, including one left open long enough to read as
carried forward and stale; and the admin cannot punch while one employee works
from home, so both halves of the access split are visible.

Not for production. The accounts below have published passwords.
"""

import argparse
import asyncio
import random
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.datetime import date_key, month_range, org_today, utc_now  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.indexes import create_indexes  # noqa: E402
from app.db.mongo import (  # noqa: E402
    Collections,
    close_mongo_connection,
    connect_to_mongo,
    get_collection,
    get_database,
)
from app.models.task import TaskProgress, TaskStatus  # noqa: E402
from app.models.user import UserRole  # noqa: E402
from app.models.work_calendar import DayKind  # noqa: E402
from app.schemas.announcement import AnnouncementCreate  # noqa: E402
from app.schemas.correction import CorrectionCreate  # noqa: E402
from app.schemas.holiday import HolidayCreate  # noqa: E402
from app.schemas.leave import LeaveApply, LeaveDecision  # noqa: E402
from app.schemas.leave_type import LeaveTypeCreate  # noqa: E402
from app.schemas.salary import SalaryCreate  # noqa: E402
from app.schemas.shift import ShiftCreate  # noqa: E402
from app.schemas.task import TaskCreate, TaskReview  # noqa: E402
from app.schemas.work_calendar import OverrideCreate, SwapCreate  # noqa: E402
from app.services import announcement as announcement_service  # noqa: E402
from app.services import attendance as attendance_service  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services import correction as correction_service  # noqa: E402
from app.services import holiday as holiday_service  # noqa: E402
from app.services import leave as leave_service  # noqa: E402
from app.services import leave_type as leave_type_service  # noqa: E402
from app.services import salary as salary_service  # noqa: E402
from app.services import settings as settings_service  # noqa: E402
from app.services import shift as shift_service  # noqa: E402
from app.services import task as task_service  # noqa: E402
from app.services import user as user_service  # noqa: E402
from app.services import work_calendar as calendar_service  # noqa: E402
from app.services.audit import SYSTEM, Actor  # noqa: E402

ADMIN = {
    "email": "admin@softus.io",
    "password": "Admin@1234",
    "first_name": "Priya",
    "last_name": "Nair",
    "designation": "HR Manager",
    "department": "People",
}

EMPLOYEES = [
    {
        "email": "riya@softus.io",
        "password": "Employee@123",
        "first_name": "Riya",
        "last_name": "Shah",
        "designation": "Senior Engineer",
        "department": "Engineering",
        "employee_code": "EMP-1001",
    },
    {
        "email": "arjun@softus.io",
        "password": "Employee@123",
        "first_name": "Arjun",
        "last_name": "Mehta",
        "designation": "Engineer",
        "department": "Engineering",
        "employee_code": "EMP-1002",
    },
    {
        "email": "sana@softus.io",
        "password": "Employee@123",
        "first_name": "Sana",
        "last_name": "Kulkarni",
        "designation": "Designer",
        "department": "Design",
        "employee_code": "EMP-1003",
    },
    {
        "email": "vikram@softus.io",
        "password": "Employee@123",
        "first_name": "Vikram",
        "last_name": "Iyer",
        "designation": "QA Engineer",
        "department": "Engineering",
        "employee_code": "EMP-1004",
    },
]

OFFICE = {
    "latitude": 23.0225,
    "longitude": 72.5714,
    "accuracy_metres": 14.0,
    "address": "Prahlad Nagar, Ahmedabad",
}

#: Monthly CTC in rupees, effective a given number of days ago. Riya has two
#: rows because a raise adds a record rather than overwriting one, and a history
#: with a single entry never shows that.
#:
#: Vikram is missing on purpose: the salary screen warns about an employee with
#: no figure on record, and an organisation where everybody is paid would never
#: show it.
SALARIES = {
    "riya@softus.io": [(400, 78_000), (120, 90_000)],
    "arjun@softus.io": [(400, 62_000)],
    "sana@softus.io": [(400, 48_000)],
}

#: The day's work, spread across every progress marker an employee can set.
#: ``(employee, days ago, progress, title, description, minutes)``. The first
#: entry is logged three and a half weeks back and never closed — that is what
#: makes "carried forward" and "stale" appear on the task explorer.
TASK_PLAN = [
    (0, 24, TaskProgress.IN_PROGRESS, "Migrating the reporting warehouse",
     "Vendor schema keeps moving; still the oldest thing open.", 240),
    (1, 6, TaskProgress.DONE, "Fixed the calendar legend",
     "Colours were not matching the status labels.", 180),
    (2, 4, TaskProgress.DONE, "Redrew the empty states",
     "Every list now says what to do when it has nothing in it.", 210),
    (3, 2, TaskProgress.BLOCKED, "Regression pass on the punch flow",
     "Waiting on a staging build with the device gate switched on.", 120),
    (0, 1, TaskProgress.IN_PROGRESS, "Paired on the leave balance bug",
     "Reserved days were not released on rejection.", 300),
    (1, 0, TaskProgress.TO_DO, "Sprint planning",
     "Estimating the correction workflow.", 90),
    (2, 0, TaskProgress.DONE, "Reviewed the attendance API",
     "Went through the punch endpoints and left comments.", 195),
    (3, 0, TaskProgress.IN_PROGRESS, "Customer call follow-up",
     "Writing up the notes and raising two tickets.", 150),
]


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """A local wall-clock time on a given day, stored as UTC."""
    return datetime.combine(day, time(hour, minute), tzinfo=settings.org_tz).astimezone(UTC)


def punch(moment: datetime) -> dict:
    return {
        "at": moment,
        **OFFICE,
        "ip": "203.0.113.24",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
        "device_class": "DESKTOP",
        "source": "WEB",
        "note": None,
    }


async def ensure_configuration() -> None:
    await settings_service.get_settings(refresh=True)

    if await shift_service.get_default_shift() is None:
        await shift_service.create_shift(
            ShiftCreate(
                name="General Shift",
                code="GENERAL",
                start_time="09:30",
                end_time="18:30",
                required_minutes=540,
                half_day_minutes=240,
                grace_minutes=15,
                working_weekdays=[0, 1, 2, 3, 4],
                is_default=True,
                description="Standard Monday to Friday shift.",
            ),
            SYSTEM,
        )
        print("  shift        : General Shift created")

    existing = {item["code"] for item in await leave_type_service.list_leave_types()}
    defaults = [
        LeaveTypeCreate(name="Casual Leave", code="CL", annual_quota=12, accrual="MONTHLY",
                        color="#2563eb", sort_order=1),
        LeaveTypeCreate(name="Sick Leave", code="SL", annual_quota=12, color="#dc2626",
                        sort_order=2),
        LeaveTypeCreate(name="Earned Leave", code="EL", annual_quota=15, accrual="MONTHLY",
                        carry_forward_max=15, min_notice_days=7, color="#16a34a", sort_order=3),
        LeaveTypeCreate(name="Unpaid Leave", code="LWP", annual_quota=0, is_paid=False,
                        color="#78716c", sort_order=4),
    ]
    added = 0
    for leave_type in defaults:
        if leave_type.code in existing:
            continue
        await leave_type_service.create_leave_type(leave_type, SYSTEM)
        added += 1
    print(f"  leave types  : {added} added, {len(existing)} already present")


async def ensure_holidays() -> list[str]:
    """A holiday last week and one coming up, so both states are visible."""
    today = org_today()
    wanted = [
        (today - timedelta(days=today.weekday() + 3), "Founders Day"),
        (today + timedelta(days=21), "Regional Festival"),
        (date(today.year, 12, 25), "Christmas"),
    ]
    created = []
    for day, name in wanted:
        try:
            await holiday_service.create_holiday(
                HolidayCreate(date=day, name=name, type="PUBLIC"), SYSTEM
            )
            created.append(date_key(day))
        except AppError:
            pass  # already listed
    print(f"  holidays     : {len(created)} added")
    return created


async def ensure_people() -> tuple[dict, list[dict]]:
    users = get_collection(Collections.USERS)

    admin = await users.find_one({"email": ADMIN["email"]})
    if admin is None:
        admin = await auth_service.create_active_user(
            email=ADMIN["email"],
            password=ADMIN["password"],
            first_name=ADMIN["first_name"],
            last_name=ADMIN["last_name"],
            role=UserRole.ADMIN,
            designation=ADMIN["designation"],
            department=ADMIN["department"],
        )
        print(f"  admin        : {ADMIN['email']} created")
    else:
        admin = {**admin, "id": str(admin["_id"])}
        print(f"  admin        : {ADMIN['email']} already exists")

    people = []
    for spec in EMPLOYEES:
        found = await users.find_one({"email": spec["email"]})
        if found is not None:
            people.append({**found, "id": str(found["_id"])})
            continue
        created = await auth_service.create_active_user(
            email=spec["email"],
            password=spec["password"],
            first_name=spec["first_name"],
            last_name=spec["last_name"],
            designation=spec["designation"],
            department=spec["department"],
            employee_code=spec["employee_code"],
            joining_date=org_today() - timedelta(days=400),
        )
        people.append(created)
    print(f"  employees    : {len(people)} present")

    # One invitation left outstanding, so the employee list shows that state.
    if await users.find_one({"email": "newjoiner@softus.io"}) is None:
        from app.schemas.auth import InviteCreate

        await auth_service.invite_user(
            InviteCreate(
                email="newjoiner@softus.io",
                first_name="Meera",
                last_name="Rao",
                department="Design",
            ),
            invited_by=admin["id"],
        )
        print("  invitation   : newjoiner@softus.io pending")

    return admin, people


async def record_day(person: dict, day: date, sessions: list[dict], shift: dict | None) -> None:
    """Store one day's punches and let the engine decide what they mean."""
    attendance = get_collection(Collections.ATTENDANCE)
    await attendance.update_one(
        {"user_id": person["id"], "date": date_key(day)},
        {
            "$set": {
                "sessions": sessions,
                "shift_snapshot": shift,
                "shift_id": shift["id"] if shift else None,
                "is_deleted": False,
                "updated_at": utc_now(),
            },
            "$setOnInsert": {"created_at": utc_now()},
        },
        upsert=True,
    )
    await attendance_service.recompute_day(person["id"], day, user=person)


async def backfill_attendance(person: dict, holidays: set[str], weeks: int = 6) -> None:
    """Six weeks of plausible history, with a few deliberate imperfections."""
    shift = await shift_service.get_default_shift()
    today = org_today()
    seed_value = sum(ord(character) for character in person["email"])
    rng = random.Random(seed_value)

    for offset in range(weeks * 7, 0, -1):
        day = today - timedelta(days=offset)
        if day.weekday() > 4 or date_key(day) in holidays:
            continue

        roll = rng.random()
        if roll < 0.04:
            continue  # absent: no document at all
        if roll < 0.09:
            sessions = [{"punch_in": punch(at(day, 9, 40)), "punch_out": None,
                         "auto_closed": True}]  # forgot to punch out
        elif roll < 0.15:
            sessions = [{"punch_in": punch(at(day, 9, 35)),
                         "punch_out": punch(at(day, 13, 45)), "auto_closed": False}]  # half day
        elif roll < 0.55:
            # Punched out for lunch.
            start_minute = rng.choice([25, 30, 35, 48])
            sessions = [
                {"punch_in": punch(at(day, 9, start_minute)),
                 "punch_out": punch(at(day, 13, 30)), "auto_closed": False},
                {"punch_in": punch(at(day, 14, 15)),
                 "punch_out": punch(at(day, 19, rng.choice([0, 10, 25]))), "auto_closed": False},
            ]
        else:
            start_minute = rng.choice([20, 28, 33, 42, 55])
            sessions = [{"punch_in": punch(at(day, 9, start_minute)),
                         "punch_out": punch(at(day, 18, rng.choice([35, 45, 55]))),
                         "auto_closed": False}]

        await record_day(person, day, sessions, shift)


async def ensure_salaries(admin: dict, people: list[dict]) -> None:
    """Pay on record for everybody but one, so the missing-salary warning shows."""
    actor = Actor.from_user(admin, "127.0.0.1")
    today = org_today()

    for person in people:
        history = SALARIES.get(person["email"])
        if not history:
            print(f"  {person['email']:<22} no figure on record (deliberate)")
            continue
        for days_ago, amount in history:
            await salary_service.set_salary(
                person["id"],
                SalaryCreate(
                    monthly_ctc=float(amount),
                    effective_from=today - timedelta(days=days_ago),
                    note="Demo figure.",
                ),
                actor,
            )
        note = " (after one raise)" if len(history) > 1 else ""
        print(f"  {person['email']:<22} INR {history[-1][1]:,} a month{note}")


async def mark_work_from_home(admin: dict, person: dict) -> None:
    """One employee exempt from the office-network gate, for a fixed spell."""
    today = org_today()
    await user_service.update_user(
        person["id"],
        {
            "wfh_enabled": True,
            "wfh_from": today - timedelta(days=7),
            "wfh_to": today + timedelta(days=21),
        },
        Actor.from_user(admin, "127.0.0.1"),
    )
    print(f"  home working : {person['email']} until {date_key(today + timedelta(days=21))}")


async def shape_work_calendar(admin: dict, people: list[dict]) -> None:
    """A Saturday swap and a day closed by hand, plus the comp-offs they credit."""
    actor = Actor.from_user(admin, "127.0.0.1")
    today = org_today()
    first, last = month_range(today.year, today.month)
    month_days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    shift = await shift_service.get_default_shift()

    open_saturdays: list[date] = []
    shut_saturdays: list[date] = []
    for day in (candidate for candidate in month_days if candidate.weekday() == 5):
        decision = await calendar_service.decide(day)
        (open_saturdays if decision.is_working else shut_saturdays).append(day)

    # Take away a Saturday people have already worked — that is what credits the
    # comp-offs — and open one that has not arrived yet, so nobody reads as
    # absent on a day that was off while it was passing.
    off_date = next((day for day in reversed(open_saturdays) if day < today), None)
    off_date = off_date or (open_saturdays[0] if open_saturdays else None)
    working_date = next((day for day in shut_saturdays if day > today), None)
    working_date = working_date or (shut_saturdays[0] if shut_saturdays else None)

    if off_date is None or working_date is None:
        print("  swap         : skipped (this month has no working/off Saturday pair)")
    else:
        # A swap only owes a comp-off to somebody who actually worked the
        # Saturday, and the back-fill deliberately skips weekends.
        if off_date < today:
            for person in people[:2]:
                await record_day(
                    person,
                    off_date,
                    [{"punch_in": punch(at(off_date, 10, 0)),
                      "punch_out": punch(at(off_date, 15, 30)), "auto_closed": False}],
                    shift,
                )
        try:
            swap = await calendar_service.create_swap(
                SwapCreate(
                    off_date=off_date,
                    working_date=working_date,
                    reason="Team offsite — taking the later Saturday off instead.",
                ),
                actor,
            )
            print(
                f"  swap         : {swap['off_date']} off, {swap['working_date']} working"
                f" ({swap['comp_offs_credited']} comp-offs)"
            )
        except AppError as error:
            print(f"  swap         : skipped ({error.message})")

    closed: date | None = None
    for offset in range(1, 15):
        day = today - timedelta(days=offset)
        if day < first:
            break
        # Saturdays are the swap's story; this one is about an ordinary weekday.
        if day.weekday() == 5 or not await calendar_service.is_working_day(day):
            continue
        closed = day
        break

    if closed is None:
        print("  closed day   : skipped (no worked day left in this month)")
        return
    try:
        override = await calendar_service.set_override(
            OverrideCreate(
                date=closed,
                kind=DayKind.OFF,
                reason="Office shut — municipal water shutdown, nobody was expected in.",
            ),
            actor,
        )
        print(
            f"  closed day   : {date_key(closed)} closed by hand"
            f" ({override['comp_offs_credited']} comp-offs)"
        )
    except AppError as error:
        print(f"  closed day   : skipped ({error.message})")


async def seed_activity(admin: dict, people: list[dict]) -> None:
    """Work in the queues, so the approval screens have something to show."""
    riya, arjun, sana, vikram = people
    admin_actor = Actor.from_user(admin, "127.0.0.1")
    today = org_today()

    # Corrections waiting for a decision. Several, and from different people, so
    # the bulk selection has a real batch to work on rather than a single row.
    attendance = get_collection(Collections.ATTENDANCE)
    open_day = None
    for offset in range(2, 8):
        day = today - timedelta(days=offset)
        record = await attendance.find_one({"user_id": arjun["id"], "date": date_key(day)})
        if record and any(s.get("punch_out") is None for s in record.get("sessions", [])):
            open_day = day
            break
    open_day = open_day or today - timedelta(days=2)
    disputed = today - timedelta(days=3)
    late_start = today - timedelta(days=5)

    async def file_correction(person: dict, day: date, **fields) -> int:
        try:
            await correction_service.create_correction(
                person,
                CorrectionCreate(date=day, **fields),
                Actor.from_user(person, "127.0.0.1"),
            )
            return 1
        except AppError as error:
            print(f"  correction   : skipped for {person['email']} ({error.message})")
            return 0

    filed = await file_correction(
        arjun, open_day,
        type="MISSED_PUNCH_OUT",
        requested_punch_out=at(open_day, 18, 45),
        reason="Left for the client meeting and forgot to punch out.",
    )
    filed += await file_correction(
        riya, disputed,
        type="WRONG_TIME",
        requested_punch_in=at(disputed, 9, 15),
        requested_punch_out=at(disputed, 18, 40),
        reason="The reader was down; security signed us in on paper that morning.",
    )
    filed += await file_correction(
        vikram, late_start,
        type="MISSED_PUNCH_IN",
        requested_punch_in=at(late_start, 9, 25),
        reason="Came straight from the client site and only punched in after lunch.",
    )
    print(f"  corrections  : {filed} waiting for approval")

    # Tasks across every progress marker, all submitted, one already approved so
    # both sides of the admin's verdict are visible next to the employee's own.
    logged = []
    for index, days_ago, progress, title, description, minutes in TASK_PLAN:
        person = people[index]
        try:
            logged.append(
                await task_service.create_task(
                    person,
                    TaskCreate(
                        date=today - timedelta(days=days_ago),
                        title=title,
                        description=description,
                        actual_minutes=minutes,
                        project="Attendance",
                        progress=progress,
                    ),
                    Actor.from_user(person, "127.0.0.1"),
                )
            )
        except AppError as error:
            print(f"  task         : skipped '{title}' ({error.message})")
    if logged:
        settled = logged[1] if len(logged) > 1 else logged[0]
        await task_service.review(
            settled["id"], TaskStatus.APPROVED,
            TaskReview(note="Clear write-up, thanks."), admin, admin_actor,
        )
        print(f"  tasks        : {len(logged)} logged, 1 approved, {len(logged) - 1} pending")

    # Leave: one already approved, and a queue of requests still to decide.
    balances = await leave_service.list_balances(sana["id"])
    by_code = {balance["leave_type_code"]: balance for balance in balances}
    casual = by_code.get("CL")
    sick = by_code.get("SL") or casual

    async def apply(person: dict, balance: dict, start: int, span: int, reason: str) -> dict | None:
        # Nudged forward to a weekday the office is open on. A request that
        # lands entirely on days off is refused, and which weekday "in nine
        # days" is depends on when the script happens to be run. Saturdays are
        # skipped even when the calendar has one open: leave is counted against
        # the shift's weekdays, so a Saturday request would be a nought-day one.
        from_date = today + timedelta(days=start)
        for _ in range(14):
            if from_date.weekday() < 5 and await calendar_service.is_working_day(from_date):
                break
            from_date += timedelta(days=1)
        try:
            return await leave_service.apply_for_leave(
                person,
                LeaveApply(
                    leave_type_id=balance["leave_type_id"],
                    from_date=from_date,
                    to_date=from_date + timedelta(days=span - 1),
                    reason=reason,
                ),
                Actor.from_user(person, "127.0.0.1"),
            )
        except AppError as error:
            print(f"  leave        : skipped for {person['email']} ({error.message})")
            return None

    if casual:
        approved = await apply(sana, casual, 9, 2, "Family function out of town.")
        if approved:
            await leave_service.decide(
                approved["id"], True, LeaveDecision(note="Approved, enjoy."), admin, admin_actor
            )
            print("  leave        : 1 approved for next week")

        pending = [
            await apply(vikram, casual, 14, 1, "Medical appointment."),
            await apply(riya, casual, 17, 2, "Moving flat, need the two days."),
            await apply(arjun, sick, 4, 1, "Dentist, all afternoon."),
        ]
        print(f"  leave        : {sum(1 for item in pending if item)} waiting for approval")

    # Announcements and an event.
    existing = await announcement_service.list_announcements(include_unpublished=True, limit=5)
    if not existing:
        await announcement_service.create_announcement(
            AnnouncementCreate(
                title="Attendance is moving to Softus Checkin",
                body=(
                    "From this week, punch in and out here at the start and end of your day. "
                    "Punching works on a laptop or desktop only — if you forget, raise a "
                    "correction and your manager will sort it out."
                ),
                pinned=True,
            ),
            admin,
            admin_actor,
        )
        await announcement_service.create_announcement(
            AnnouncementCreate(
                title="Quarterly town hall",
                body="Roadmap, hiring plan and an open floor for questions.",
                type="EVENT",
                event_date=today + timedelta(days=6),
                event_time="16:00",
                location="Main hall, 3rd floor",
            ),
            admin,
            admin_actor,
        )
        print("  announcements: 2 posted")


async def run(args: argparse.Namespace) -> int:
    await connect_to_mongo()

    if args.reset:
        database = get_database()
        for name in await database.list_collection_names():
            await database.drop_collection(name)
        settings_service.clear_cache()
        print(f"\nDropped every collection in '{settings.MONGO_DB}'.")

    await create_indexes()

    try:
        print("\nConfiguration")
        await ensure_configuration()
        holidays = set(await ensure_holidays())

        print("\nPeople")
        admin, people = await ensure_people()

        print("\nAttendance history")
        for person in people:
            await backfill_attendance(person, holidays)
        print(f"  back-filled  : 6 weeks for {len(people)} employees")

        print("\nWork calendar")
        await shape_work_calendar(admin, people)

        print("\nSalary")
        await ensure_salaries(admin, people)

        print("\nWho may punch")
        # The administrator was created with punching off, which is the default
        # for the role; giving one employee work-from-home shows the other gate.
        print(f"  {ADMIN['email']:<22} administrator, punching off (the default)")
        await mark_work_from_home(admin, people[2])

        print("\nActivity")
        await seed_activity(admin, people)

        print("\n" + "=" * 66)
        print("  Demo sign-in details")
        print("=" * 66)
        print(f"  Administrator   {ADMIN['email']:<24} {ADMIN['password']}")
        for spec in EMPLOYEES:
            print(f"  Employee        {spec['email']:<24} {spec['password']}")
        print("=" * 66)
        print("  Open http://localhost:5173 and sign in.")
        print("  Punching only works from a laptop or desktop computer.")
        print("=" * 66 + "\n")
        return 0
    finally:
        await close_mongo_connection()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a demo organisation.")
    parser.add_argument(
        "--reset", action="store_true", help="Drop every collection before seeding."
    )
    args = parser.parse_args()
    # Published passwords, and `--reset` drops every collection. Neither belongs
    # anywhere near the organisation's real records.
    if settings.is_production:
        print("Refusing to seed demo data: ENVIRONMENT is production.")
        return 2
    setup_logging()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
