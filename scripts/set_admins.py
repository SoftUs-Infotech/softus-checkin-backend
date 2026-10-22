"""Create or update administrators directly in the database MONGO_URI points at.

    python scripts/set_admins.py                  # reads scripts/admins.local.json
    python scripts/set_admins.py --file my.json   # another file
    python scripts/set_admins.py --yes            # skip the confirmation prompt

Write the people in ``scripts/admins.local.json`` (copy
``admins.local.example.json``). That file is in .gitignore, so a password
typed into it can never be pushed. Each entry is made an active administrator:

* a new email becomes a new administrator account, ready to sign in;
* an existing account is promoted to administrator, reinstated if it was
  suspended or removed, and given the password from the file — which signs it
  out of every device.

The first run against an empty database also creates what the app needs to be
usable: organisation settings, the default shift and the standard leave types
(the same as ``seed.py``; nothing is duplicated on later runs).

Before writing, it shows which server and database it is about to change and
asks you to type the database name. Nothing is emailed: invitations and
welcome mail are not part of this.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(BACKEND_ROOT)  # so Settings finds backend/.env wherever this is run from

# This script never signs a token, so it does not need the real SECRET_KEY —
# and .env may deliberately have none. It never sends mail either.
os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["EMAIL_PROVIDER"] = "console"
# The production start-up checks are about serving traffic, not about a CLI.
os.environ["ENVIRONMENT"] = "development"

from app.core.config import settings  # noqa: E402
from app.core.datetime import utc_now  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.indexes import create_indexes  # noqa: E402
from app.db.mongo import (  # noqa: E402
    Collections,
    close_mongo_connection,
    connect_to_mongo,
    get_collection,
    redact_uri,
)
from app.models.audit_log import AuditAction, AuditEntity  # noqa: E402
from app.models.user import UserRole, UserStatus, default_can_punch  # noqa: E402
from app.schemas.auth import validate_password_strength  # noqa: E402
from app.services import audit  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services.audit import SYSTEM  # noqa: E402
from scripts.seed import seed_configuration  # noqa: E402

DEFAULT_FILE = BACKEND_ROOT / "scripts" / "admins.local.json"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_admins(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(
            f"{path} not found.\n"
            f"Copy scripts/admins.local.example.json to {path.name} and fill it in."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path.name} is not valid JSON: {exc}") from exc

    entries = data.get("admins") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f'{path.name} needs an "admins" list with at least one entry.')

    problems, admins, seen = [], [], set()
    for number, entry in enumerate(entries, start=1):
        email = str(entry.get("email", "")).strip().lower()
        password = str(entry.get("password", ""))
        first_name = str(entry.get("first_name", "")).strip()
        where = f"entry {number} ({email or 'no email'})"
        if not EMAIL.match(email):
            problems.append(f"{where}: email is missing or not an email address")
        elif email in seen:
            problems.append(f"{where}: listed twice")
        seen.add(email)
        if not first_name:
            problems.append(f"{where}: first_name is required")
        try:
            validate_password_strength(password)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
        admins.append(
            {
                "email": email,
                "password": password,
                "first_name": first_name,
                "last_name": str(entry.get("last_name", "")).strip(),
            }
        )
    if problems:
        raise SystemExit("Nothing was written. Fix these first:\n  - " + "\n  - ".join(problems))
    return admins


async def apply(admin: dict[str, str]) -> str:
    users = get_collection(Collections.USERS)
    existing = await users.find_one({"email": admin["email"]})

    if existing is None:
        created = await auth_service.create_active_user(
            email=admin["email"],
            password=admin["password"],
            first_name=admin["first_name"],
            last_name=admin["last_name"],
            role=UserRole.ADMIN,
        )
        await audit.record(
            SYSTEM,
            AuditAction.CREATE,
            AuditEntity.USER,
            entity_id=created["id"],
            entity_label=admin["email"],
            after={"role": UserRole.ADMIN.value, "source": "scripts/set_admins.py"},
        )
        return "created"

    now = utc_now()
    before = {
        "role": existing.get("role"),
        "status": existing.get("status"),
        "is_deleted": existing.get("is_deleted", False),
    }
    await users.update_one(
        {"_id": existing["_id"]},
        {
            "$set": {
                "hashed_password": hash_password(admin["password"]),
                "first_name": admin["first_name"],
                "last_name": admin["last_name"],
                "role": UserRole.ADMIN.value,
                "can_punch": existing.get("can_punch", default_can_punch(UserRole.ADMIN)),
                "status": UserStatus.ACTIVE.value,
                "is_deleted": False,
                "profile_completed": True,
                "activated_at": existing.get("activated_at") or now,
                "updated_at": now,
            }
        },
    )
    # A new password should end the old sessions, as a password change does.
    await get_collection(Collections.REFRESH_TOKENS).update_many(
        {"user_id": str(existing["_id"]), "revoked_at": None}, {"$set": {"revoked_at": now}}
    )
    await audit.record(
        SYSTEM,
        AuditAction.UPDATE,
        AuditEntity.USER,
        entity_id=str(existing["_id"]),
        entity_label=admin["email"],
        before=before,
        after={
            "role": UserRole.ADMIN.value,
            "status": UserStatus.ACTIVE.value,
            "is_deleted": False,
            "password": "reset by scripts/set_admins.py",
        },
    )
    if before["is_deleted"]:
        return "restored (had been removed) and password reset"
    if before["role"] != UserRole.ADMIN.value:
        return f"promoted from {before['role']} to administrator, password reset"
    return "password reset, active"


async def run(args: argparse.Namespace) -> int:
    admins = load_admins(Path(args.file))

    print("\nAbout to write to:")
    print(f"  server   : {redact_uri(settings.MONGO_URI)}")
    print(f"  database : {settings.MONGO_DB}")
    print(f"  admins   : {', '.join(admin['email'] for admin in admins)}")

    await connect_to_mongo()
    try:
        users = get_collection(Collections.USERS)
        total = await users.count_documents({"is_deleted": False})
        active_admins = await users.count_documents(
            {"role": "ADMIN", "status": "ACTIVE", "is_deleted": False}
        )
        print(f"  existing : {total} account(s), {active_admins} active administrator(s)")

        if not args.yes:
            typed = input(f"\nType the database name ({settings.MONGO_DB}) to continue: ")
            if typed.strip() != settings.MONGO_DB:
                print("Not confirmed. Nothing was written.")
                return 1

        await create_indexes()
        print("\nOrganisation setup:")
        await seed_configuration()

        print("\nAdministrators:")
        for admin in admins:
            outcome = await apply(admin)
            print(f"  {admin['email']:<36} {outcome}")
        print("\nDone. Sign in with the email and password from the file.\n")
        return 0
    finally:
        await close_mongo_connection()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update administrators.")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="JSON file of admins.")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
