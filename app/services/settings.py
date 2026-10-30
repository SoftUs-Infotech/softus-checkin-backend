"""Organisation settings.

A single document, created on first read. Cached in-process because the
attendance engine reads it on every punch; the cache is cleared on write.

The cache is per worker, so with multiple workers a settings change can take up
to one request to appear everywhere. That is acceptable for policy values that
change a few times a year.
"""

from datetime import date
from typing import Any

from app.core.datetime import date_key, org_today, utc_now
from app.core.exceptions import ValidationError
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.models.settings import SETTINGS_KEY, OrgSettings
from app.schemas.settings import OrgSettingsUpdate
from app.services import audit
from app.services.audit import Actor

_cache: dict[str, Any] | None = None


def _settings():
    return get_collection(Collections.SETTINGS)


def clear_cache() -> None:
    global _cache
    _cache = None


async def get_settings(*, refresh: bool = False) -> dict[str, Any]:
    global _cache
    if _cache is not None and not refresh:
        return _cache

    document = await _settings().find_one({"key": SETTINGS_KEY})
    if document is None:
        defaults = OrgSettings()
        await _settings().insert_one(defaults.to_mongo())
        document = await _settings().find_one({"key": SETTINGS_KEY})

    _cache = serialize(document)
    return _cache


async def set_tracking_start(starts_on: date | None, actor: Actor) -> dict[str, Any]:
    """Start counting attendance from ``starts_on`` (today when omitted).

    Nothing is deleted: punches recorded before the day stay in the database and
    simply stop counting, so a wrong date is undone by setting the right one.
    A future date is refused — today would stop counting for everybody.
    """
    today = org_today()
    day = starts_on or today
    if day > today:
        raise ValidationError(
            "Tracking cannot start in the future. Reset on the day you go live.",
            code="TRACKING_START_IN_FUTURE",
        )

    existing = await get_settings(refresh=True)
    await _settings().update_one(
        {"key": SETTINGS_KEY},
        {"$set": {"tracking_starts_on": date_key(day), "updated_at": utc_now()}},
    )
    clear_cache()
    updated = await get_settings(refresh=True)
    await audit.record(
        actor,
        AuditAction.UPDATE,
        AuditEntity.SETTINGS,
        entity_id=SETTINGS_KEY,
        entity_label="Attendance tracking start",
        before={"tracking_starts_on": existing.get("tracking_starts_on")},
        after={"tracking_starts_on": date_key(day)},
    )
    return updated


async def tracking_start() -> date | None:
    """The first day attendance counts, or None when it always has."""
    value = (await get_settings()).get("tracking_starts_on")
    return date.fromisoformat(value) if value else None


async def update_settings(payload: OrgSettingsUpdate, actor: Actor) -> dict[str, Any]:
    existing = await get_settings(refresh=True)
    changes = {
        key: value for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

    merged = {**existing, **changes}
    if merged.get("enforce_office_location_for_punch") and (
        merged.get("office_latitude") is None or merged.get("office_longitude") is None
    ):
        # Enforcement with no office to measure from would refuse every punch
        # in the organisation, which nobody switching it on means to do.
        raise ValidationError(
            "Set the office location before requiring punches to be made there.",
            code="OFFICE_LOCATION_NOT_SET",
        )
    if merged.get("enforce_office_location_for_punch"):
        # The location rule replaces the IP rule for punching. Switching the
        # IP rule off here keeps the settings screen honest about which one
        # is deciding, instead of showing a rule that no longer applies.
        changes["enforce_office_network_for_punch"] = False

    changes["updated_at"] = utc_now()
    await _settings().update_one({"key": SETTINGS_KEY}, {"$set": changes})
    clear_cache()

    updated = await get_settings(refresh=True)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.SETTINGS,
        entity_id=SETTINGS_KEY,
        entity_label="Organisation settings",
        before=existing,
        after=updated,
    )
    return updated
