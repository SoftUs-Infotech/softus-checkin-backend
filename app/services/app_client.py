"""Remembering which app each person last used — shown on the admin's Employees page.

Every signed-in request reports its client (see :mod:`app.utils.client`). Writing
that on every request would double the database traffic for no reason, so a
person's row is only rewritten when what they use changes, or every few minutes
to keep "last seen" honest.
"""

import time
from typing import Any

from bson import ObjectId

from app.core.datetime import utc_now
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.utils.client import ClientInfo

logger = get_logger(__name__)

#: Rewrite "last seen" at most this often for an unchanged client.
REFRESH_SECONDS = 300

#: (user id, kind) → (what was written, when, monotonic seconds).
_written: dict[tuple[str, str], tuple[tuple, float]] = {}


def reset() -> None:
    """Test helper: forget what has been written."""
    _written.clear()


async def note(user: dict[str, Any], client: ClientInfo) -> None:
    user_id = user.get("id") or str(user.get("_id"))
    if not user_id or not ObjectId.is_valid(user_id):
        return
    now = utc_now()
    if client.is_desktop:
        signature: tuple = (client.version, client.os, client.arch, client.os_version)
        update = {
            "desktop_app": {
                "version": client.version,
                "os": client.os,
                "arch": client.arch,
                "os_version": client.os_version,
                "last_seen_at": now,
            }
        }
    else:
        signature = ()
        update = {"web_last_seen_at": now}

    key = (user_id, client.kind)
    previous = _written.get(key)
    if previous and previous[0] == signature and time.monotonic() - previous[1] < REFRESH_SECONDS:
        return
    _written[key] = (signature, time.monotonic())
    try:
        await get_collection(Collections.USERS).update_one(
            {"_id": ObjectId(user_id)}, {"$set": update}
        )
    except Exception:  # noqa: BLE001 - bookkeeping must never fail a request
        _written.pop(key, None)
        logger.exception("Could not record the client for %s", user_id)
