"""Seed a fresh database with the first administrator.

    python scripts/seed.py
    python scripts/seed.py --email admin@acme.com --password "Str0ngPass" --force

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
from app.services import auth as auth_service  # noqa: E402
from app.services import user as user_service  # noqa: E402


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


async def seed(args: argparse.Namespace) -> int:
    await connect_to_mongo()
    await create_indexes()
    try:
        existing = await user_service.count_admins()
        if existing and not args.force:
            print(
                f"An administrator already exists ({existing} active). "
                "Nothing to do â€” pass --force to add another."
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
