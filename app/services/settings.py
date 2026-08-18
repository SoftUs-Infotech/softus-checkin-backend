"""Organisation settings.

A single document, created on first read. Cached in-process because the
attendance engine reads it on every punch; the cache is cleared on write.

The cache is per worker, so with multiple workers a settings change can take up
to one request to appear everywhere. That is acceptable for policy values that
change a few times a year.
"""

from typing import Any

from app.core.datetime import utc_now
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


async def update_settings(payload: OrgSettingsUpdate, actor: Actor) -> dict[str, Any]:
    existing = await get_settings(refresh=True)
    changes = {
        key: value for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

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
