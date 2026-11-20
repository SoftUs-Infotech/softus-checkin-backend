"""The app log: what happened with a work-from-home employee's app, for administrators.

Written by the desktop app (``POST /activity/events``) and by the server
(punches, automatic punch-outs, updates); read back as one list per person per
day, with long screen locks added from the activity minutes. The employee never
sees it. Lines are kept for ``RETENTION_DAYS`` (a TTL index removes older ones).
"""

from datetime import date, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core import rate_limit
from app.core.datetime import attendance_date, date_key, to_org_tz, to_utc, utc_now
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.app_event import APP_REPORTED, AppEvent, AppEventKind, AppEventSource
from app.utils.client import ClientInfo

logger = get_logger(__name__)

RETENTION_DAYS = 90

#: Events are accepted this long after the fact (the app keeps unsent ones
#: through a lost connection) and never from the future.
ACCEPT_AGE = timedelta(hours=48)
ACCEPT_AHEAD = timedelta(minutes=2)

#: The app sends a handful an hour; this only stops a runaway loop.
REPORT_LIMIT = rate_limit.RateLimit(120, 600, "Too many log entries. Try again later.")

#: A screen locked at least this long shows as one line.
LOCKED_SPAN_MINUTES = 5

#: Why the system ended a session (stored as ``closed_detail`` on the session).
SETTING_OFF = "SETTING_OFF"
SCREEN_NOT_VISIBLE = "SCREEN_NOT_VISIBLE"
NO_SCREENSHOTS = "NO_SCREENSHOTS"
NO_CHECKINS = "NO_CHECKINS"
APP_CLOSED = "APP_CLOSED"
SHARING_STOPPED = "SHARING_STOPPED"
WORK_MODE_CHANGED = "WORK_MODE_CHANGED"
IDLE = "IDLE"

_REASON_TEXT = {
    SETTING_OFF: "a setting was turned off",
    SCREEN_NOT_VISIBLE: "the screen could not be seen",
    NO_SCREENSHOTS: "no screenshot arrived for {minutes} min",
    NO_CHECKINS: "the app stopped checking in (closed, crashed, offline or asleep)",
    APP_CLOSED: "the app was closed",
    SHARING_STOPPED: "screen sharing stopped (page closed or sharing ended)",
    WORK_MODE_CHANGED: "switched to work from home while punched in",
    IDLE: "no keyboard or mouse use for {minutes} min",
}

#: Detail keys an app may send, and how long a text value may be.
_DETAIL_KEYS = ("setting", "state", "first", "reported", "seconds", "error", "title")
_DETAIL_TEXT_MAX = 160


def _events():
    return get_collection(Collections.APP_EVENTS)


def _activity():
    return get_collection(Collections.ACTIVITY)


def _clean_details(details: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in _DETAIL_KEYS:
        value = (details or {}).get(key)
        if isinstance(value, bool) or (isinstance(value, int) and abs(value) < 10**9):
            clean[key] = value
        elif isinstance(value, str) and value.strip():
            clean[key] = value.strip()[:_DETAIL_TEXT_MAX]
    return clean


def _document(
    user_id: str,
    kind: AppEventKind,
    *,
    at: datetime,
    source: AppEventSource,
    details: dict[str, Any],
    client: ClientInfo | None,
) -> dict[str, Any]:
    return AppEvent(
        user_id=user_id,
        date=date_key(attendance_date(at)),
        at=at,
        kind=kind,
        source=source,
        details=details,
        app_version=client.version if client and client.is_desktop else None,
        os=client.os if client and client.is_desktop else None,
        os_version=client.os_version if client and client.is_desktop else None,
    ).to_mongo()


async def record(
    user_id: str,
    kind: AppEventKind,
    *,
    at: datetime | None = None,
    details: dict[str, Any] | None = None,
    client: ClientInfo | None = None,
) -> None:
    """A line the server writes. Never fails the operation that wrote it."""
    try:
        await _events().insert_one(
            _document(
                user_id,
                kind,
                at=to_utc(at) if at else utc_now(),
                source=AppEventSource.SERVER,
                details=dict(details or {}),
                client=client,
            )
        )
    except Exception:  # noqa: BLE001 - bookkeeping must never break a punch
        logger.exception("Could not write the app log for %s", user_id)


async def save_reported(
    user: dict[str, Any], events: list[dict[str, Any]], client: ClientInfo
) -> dict[str, int]:
    """Lines the app reports about itself. Only the app's own kinds are taken."""
    rate_limit.hit("app_log_report", user["id"], REPORT_LIMIT)
    now = utc_now()
    documents = []
    ignored = 0
    for event in events:
        at = to_utc(event["at"])
        kind = event["kind"]
        if kind not in APP_REPORTED or at > now + ACCEPT_AHEAD or at < now - ACCEPT_AGE:
            ignored += 1
            continue
        documents.append(
            _document(
                user["id"],
                AppEventKind(kind),
                at=min(at, now),
                source=AppEventSource.APP,
                details=_clean_details(event.get("details")),
                client=client,
            )
        )
    if documents:
        await _events().insert_many(documents, ordered=False)
    return {"accepted": len(documents), "ignored": ignored}


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _macos_major(os_version: str | None) -> int:
    try:
        return int(str(os_version or "").split(".")[0])
    except ValueError:
        return 0


def _setting_label(setting: str | None, os_name: str | None, os_version: str | None) -> str:
    if os_name == "linux":
        return "Screen choice" if setting == "screen" else "Activity helper"
    if setting == "input":
        return "Accessibility"
    # macOS 15 renamed the pane.
    return (
        "Screen & System Audio Recording" if _macos_major(os_version) >= 15 else "Screen Recording"
    )


def _os_label(os_name: str | None, os_version: str | None) -> str:
    if os_name == "darwin":
        return f"macOS {os_version}".strip() if os_version else "macOS"
    if os_name == "win32":
        return os_version or "Windows"
    if os_name == "linux":
        return f"Linux {os_version or ''}".strip()
    return ""


def _instant(value: Any) -> datetime | None:
    try:
        return to_utc(datetime.fromisoformat(str(value))) if value else None
    except ValueError:
        return None


def _duration(minutes: int) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    return f"{hours} h" if hours else f"{rest} min"


def message_for(event: dict[str, Any]) -> str:
    kind = event["kind"]
    details = event.get("details") or {}
    os_name, os_version = event.get("os"), event.get("os_version")
    label = _setting_label(details.get("setting"), os_name, os_version)
    if kind == AppEventKind.APP_OPENED:
        about = ", ".join(
            part for part in (event.get("app_version"), _os_label(os_name, os_version)) if part
        )
        return f"App opened — {about}" if about else "App opened"
    if kind == AppEventKind.APP_CLOSED:
        return "App closed"
    if kind == AppEventKind.SETTING_CHANGED:
        allowed = details.get("state") == "granted"
        if details.get("first"):
            return f"{label}: {'allowed' if allowed else 'not allowed'}"
        return f"{label}: {'turned on' if allowed else 'turned off'}"
    if kind == AppEventKind.SCREEN_NOT_VISIBLE:
        seconds = int(details.get("seconds") or 60)
        text = f"Screen could not be seen for {_duration(max(1, round(seconds / 60)))}"
        reported = details.get("reported")
        if os_name == "darwin" and reported in ("on", "off"):
            text += f" — macOS reports {label}: {reported.upper()}"
        return text
    if kind == AppEventKind.SCREENSHOT_FAILED:
        error = details.get("error")
        text = "A screenshot could not be taken"
        return f"{text} ({error})" if error else text
    if kind == AppEventKind.POPUP_SHOWN:
        return f"Popup shown: “{details.get('title') or 'Please allow this setting'}”"
    if kind == AppEventKind.PUNCHED_IN:
        return f"Punched in ({details.get('via') or 'app'})"
    if kind == AppEventKind.PUNCHED_OUT:
        return f"Punched out ({details.get('via') or 'app'})"
    if kind == AppEventKind.AUTO_PUNCHED_OUT:
        reason = _REASON_TEXT.get(details.get("reason") or "", "the screen stopped being shared")
        text = f"Punched out automatically — {reason.format(minutes=details.get('minutes') or '?')}"
        ended = _instant(details.get("ended_at"))
        if ended is not None and to_utc(event["at"]) - ended >= timedelta(minutes=1):
            text += f" (session ended {to_org_tz(ended):%H:%M})"
        return text
    if kind == AppEventKind.APP_UPDATED:
        return f"App updated {details.get('from')} → {details.get('to')}"
    return str(kind)


async def _locked_spans(user_id: str, day: date) -> list[dict[str, Any]]:
    """Runs of minutes with the screen locked for most of the minute."""
    spans: list[dict[str, Any]] = []
    start = previous = None
    cursor = (
        _activity()
        .find(
            {"user_id": user_id, "date": date_key(day), "locked_seconds": {"$gte": 30}},
            {"minute": 1},
        )
        .sort("minute", 1)
    )

    def close() -> None:
        if start is not None and previous is not None:
            minutes = int((previous - start).total_seconds() // 60) + 1
            if minutes >= LOCKED_SPAN_MINUTES:
                spans.append(
                    {
                        "at": start,
                        "until": previous + timedelta(minutes=1),
                        "kind": "SCREEN_LOCKED",
                        "source": "server",
                        "message": f"Screen locked ({_duration(minutes)})",
                    }
                )

    async for row in cursor:
        minute = to_utc(row["minute"])
        if previous is None or minute - previous > timedelta(minutes=1):
            close()
            start = minute
        previous = minute
    close()
    return spans


async def log_for(user_id: str, day: date) -> list[dict[str, Any]]:
    """One person's log for a day, oldest first."""
    if not ObjectId.is_valid(user_id):
        raise NotFoundError("That employee does not exist.", code="USER_NOT_FOUND")
    entries = [
        {
            "at": to_utc(event["at"]),
            "until": None,
            "kind": event["kind"],
            "source": event.get("source", "server"),
            "message": message_for(event),
        }
        async for event in _events()
        .find({"user_id": user_id, "date": date_key(day)})
        .sort("at", 1)
        .limit(2000)
    ]
    entries.extend(await _locked_spans(user_id, day))
    entries.sort(key=lambda entry: entry["at"])
    return entries
