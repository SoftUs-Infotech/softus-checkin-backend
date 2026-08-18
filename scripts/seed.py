"""Seed a fresh database so a clone is usable immediately.

    python scripts/seed.py
    python scripts/seed.py --email admin@acme.com --password "Str0ngPass" --force

Creates the first administrator, a default shift, a standard set of leave types
and the organisation settings document. Every step is idempotent — running it
twice adds nothing.

Without ``--password`` a strong one is generated and printed once.
"""

import argparse
import asyncio
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.exceptions import ConflictError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.indexes import create_indexes  # noqa: E402
from app.db.mongo import close_mongo_connection, connect_to_mongo  # noqa: E402
from app.schemas.leave_type import LeaveTypeCreate  # noqa: E402
from app.schemas.shift import ShiftCreate  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services import leave_type as leave_type_service  # noqa: E402
from app.services import settings as settings_service  # noqa: E402
from app.services import shift as shift_service  # noqa: E402
from app.services import user as user_service  # noqa: E402
from app.services.audit import SYSTEM  # noqa: E402

DEFAULT_SHIFT = ShiftCreate(
    name="General Shift",
    code="GENERAL",
    start_time="09:30",
    end_time="18:30",
    required_minutes=540,  # 9 hours
    half_day_minutes=240,  # 4 hours
    grace_minutes=15,
    working_weekdays=[0, 1, 2, 3, 4],
    is_default=True,
    description="Standard Monday–Friday shift.",
)

DEFAULT_LEAVE_TYPES = [
    LeaveTypeCreate(
        name="Casual Leave", code="CL", annual_quota=12, accrual="MONTHLY",
        carry_forward_max=0, color="#2563eb", sort_order=1,
    ),
    LeaveTypeCreate(
        name="Sick Leave", code="SL", annual_quota=12, accrual="YEARLY",
        carry_forward_max=0, min_notice_days=0, color="#dc2626", sort_order=2,
    ),
    LeaveTypeCreate(
        name="Earned Leave", code="EL", annual_quota=15, accrual="MONTHLY",
        carry_forward_max=15, min_notice_days=7, color="#16a34a", sort_order=3,
    ),
    LeaveTypeCreate(
        name="Unpaid Leave", code="LWP", annual_quota=0, accrual="YEARLY",
        is_paid=False, color="#78716c", sort_order=4,
        description="Leave without pay, for when the balance is exhausted.",
    ),
]


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


async def seed_configuration() -> None:
    """Default shift, leave types and settings. Safe to run repeatedly."""
    await settings_service.get_settings(refresh=True)
    print("  settings      : ready")

    if await shift_service.get_default_shift() is None:
        shift = await shift_service.create_shift(DEFAULT_SHIFT, SYSTEM)
        print(f"  default shift : created '{shift['name']}' ({shift['code']})")
    else:
        print("  default shift : already present")

    existing_types = {item["code"] for item in await leave_type_service.list_leave_types()}
    added = 0
    for leave_type in DEFAULT_LEAVE_TYPES:
        if leave_type.code in existing_types:
            continue
        await leave_type_service.create_leave_type(leave_type, SYSTEM)
        added += 1
    print(f"  leave types   : {added} added, {len(existing_types)} already present")


async def seed(args: argparse.Namespace) -> int:
    await connect_to_mongo()
    await create_indexes()
    try:
        print("\nSeeding configuration:")
        await seed_configuration()

        existing = await user_service.count_admins()
        if existing and not args.force:
            print(
                f"\nAn administrator already exists ({existing} active). "
                "Pass --force to add another.\n"
            )
            return 0

        password = args.password or generate_password()
        try:
            admin = await auth_service.bootstrap_admin(
                email=args.email,
                password=password,
                first_name=args.first_name,
                last_name=args.last_name,
            )
        except ConflictError as exc:
            print(f"Could not create the administrator: {exc.message}")
            return 1

        print("\n" + "=" * 62)
        print("  Administrator created")
        print("=" * 62)
        print(f"  Email    : {admin['email']}")
        print(f"  Password : {password}")
        print("=" * 62)
        if not args.password:
            print("  This password is shown once. Store it now, then change it after")
            print("  signing in.")
            print("=" * 62 + "\n")
        return 0
    finally:
        await close_mongo_connection()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the first administrator.")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default=None, help="Generated if omitted.")
    parser.add_argument("--first-name", dest="first_name", default="System")
    parser.add_argument("--last-name", dest="last_name", default="Administrator")
    parser.add_argument(
        "--force", action="store_true", help="Create even if an administrator exists."
    )
    args = parser.parse_args()

    setup_logging()
    return asyncio.run(seed(args))


if __name__ == "__main__":
    raise SystemExit(main())
