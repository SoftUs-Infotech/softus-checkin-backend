"""Work-from-home activity: what the desktop app measured, minute by minute.

The app counts — never records — keyboard and mouse use anywhere on the
computer, compares tiny copies of the screen with themselves a few seconds
apart, and notes when the screen is locked. It sends one row per minute while
the person is punched in. The admin page reads them back added up per
screenshot window, with the patterns worth a second look flagged.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne

from app.core.datetime import attendance_date, date_key, to_utc, utc_now
from app.core.exceptions import NotFoundError
from app.db.mongo import Collections, get_collection
from app.schemas.activity import ActivityUpload
from app.services import screenshot as screenshot_service
from app.utils.client import ClientInfo

#: Minutes are accepted this long after the fact — the app keeps unsent ones
#: through a lost connection — and never from the future.
ACCEPT_AGE = timedelta(hours=48)
ACCEPT_AHEAD = timedelta(minutes=2)

_FIGURES = (
    "active_seconds",
    "keys",
    "clicks",
    "scrolls",
    "mouse_distance",
    "screen_samples",
    "screen_changes",
    "locked_seconds",
    "virtual_seconds",
)


def _rows():
    return get_collection(Collections.ACTIVITY)


async def save(user: dict[str, Any], payload: ActivityUpload, client: ClientInfo) -> dict[str, int]:
    screenshot_service.require_wfh(user)
    now = utc_now()
    operations: list[UpdateOne] = []
    ignored = 0
    seen: set = set()
    for item in payload.minutes:
        minute = to_utc(item.minute).replace(second=0, microsecond=0)
        if minute > now + ACCEPT_AHEAD or minute < now - ACCEPT_AGE or minute in seen:
            ignored += 1
            continue
        seen.add(minute)
        figures = {name: getattr(item, name) for name in _FIGURES}
        operations.append(
            UpdateOne(
                {"user_id": user["id"], "minute": minute},
                {
                    "$set": {
                        **figures,
                        "date": date_key(attendance_date(minute)),
                        "app_version": client.version,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now, "is_deleted": False},
                },
                upsert=True,
            )
        )
    if operations:
        await _rows().bulk_write(operations, ordered=False)
    return {"accepted": len(operations), "ignored": ignored}


def _zero() -> dict[str, int]:
    return {"tracked_minutes": 0, **{name: 0 for name in _FIGURES}}


def _percent(figures: dict[str, int]) -> int:
    tracked = figures["tracked_minutes"] * 60
    return round(100 * figures["active_seconds"] / tracked) if tracked else 0


def _flags(figures: dict[str, int], percent: int) -> list[str]:
    tracked = figures["tracked_minutes"] * 60
    flags: list[str] = []
    if tracked and figures["locked_seconds"] >= tracked / 2:
        flags.append("locked")
    elif percent < 15:
        flags.append("idle")
    no_typing = figures["keys"] == 0 and figures["clicks"] == 0 and figures["scrolls"] == 0
    sampled = figures["screen_samples"] >= 3
    # Input all window long, yet not one key, click or scroll and a screen that
    # never changed: the pointer is being moved for the sake of it.
    if (
        percent >= 30
        and no_typing
        and figures["mouse_distance"] > 0
        and sampled
        and figures["screen_changes"] == 0
    ):
        flags.append("mouse_only")
    # Input made by a program rather than a hand on a keyboard or mouse.
    if figures["virtual_seconds"] >= 30 and figures["virtual_seconds"] >= 0.3 * max(
        1, figures["active_seconds"]
    ):
        flags.append("virtual_input")
    # The screen keeps changing with nobody touching anything: a video, a slideshow.
    if sampled and figures["screen_changes"] >= 0.6 * figures["screen_samples"] and percent < 5:
        flags.append("screen_only")
    return flags


async def day(user_id: str, on: date) -> dict[str, Any]:
    if not ObjectId.is_valid(user_id):
        raise NotFoundError("That employee does not exist.", code="USER_NOT_FOUND")
    interval = await screenshot_service.interval_minutes()
    span = interval * 60
    windows: dict[int, dict[str, int]] = {}
    totals = _zero()
    async for row in _rows().find({"user_id": user_id, "date": date_key(on)}).sort("minute", 1):
        stamp = int(to_utc(row["minute"]).timestamp())
        bucket = windows.setdefault(stamp - stamp % span, _zero())
        for target in (bucket, totals):
            target["tracked_minutes"] += 1
            for name in _FIGURES:
                target[name] += int(row.get(name) or 0)

    out = []
    for start in sorted(windows):
        figures = windows[start]
        percent = _percent(figures)
        begins = datetime.fromtimestamp(start, tz=UTC)
        out.append(
            {
                **figures,
                "start": begins,
                "end": begins + timedelta(seconds=span),
                "active_percent": percent,
                "level": "high" if percent >= 50 else "medium" if percent >= 20 else "low",
                "flags": _flags(figures, percent),
            }
        )
    return {
        "date": date_key(on),
        "interval_minutes": interval,
        "totals": totals,
        "active_percent": _percent(totals),
        "windows": out,
    }
