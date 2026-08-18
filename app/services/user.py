"""User lookups.

The only layer that talks to the ``users`` collection. Registration, invites and
admin management land here in the next phase; for now this holds the reads that
authentication depends on.
"""

from typing import Any

from bson import ObjectId

from app.core.exceptions import NotFoundError
from app.db.mongo import Collections, get_collection
from app.models.base import serialize


def _users():
    return get_collection(Collections.USERS)


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    if not ObjectId.is_valid(user_id):
        return None
    document = await _users().find_one({"_id": ObjectId(user_id), "is_deleted": False})
    return serialize(document)


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    document = await _users().find_one(
        {"email": email.strip().lower(), "is_deleted": False}
    )
    return serialize(document)


async def require_user(user_id: str) -> dict[str, Any]:
    user = await get_user_by_id(user_id)
    if user is None:
        raise NotFoundError("This user account no longer exists.", code="USER_NOT_FOUND")
    return user
