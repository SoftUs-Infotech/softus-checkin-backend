"""The audit trail.

Append-only. ``record`` never raises: a failure to write history must not undo
the change it was describing — it is logged and swallowed.

``Actor`` travels from the route into the service so the service layer stays the
only place that touches the database, without routes needing to know what an
audit row looks like.
"""

from dataclasses import dataclass
from typing import Any

from bson import ObjectId

from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity, AuditLog
from app.models.base import serialize
from app.schemas.common import PaginationParams

logger = get_logger(__name__)

# Never copied into an audit row, however the caller passes them in.
_REDACTED_FIELDS = {"hashed_password", "token_hash", "password", "new_password"}


@dataclass(frozen=True, slots=True)
class Actor:
    """Who performed an action, and from where."""

    id: str | None = None
    email: str | None = None
    ip: str | None = None

    @classmethod
    def from_user(cls, user: dict[str, Any] | None, ip: str | None = None) -> "Actor":
        if user is None:
            return cls(ip=ip)
        return cls(id=user.get("id"), email=user.get("email"), ip=ip)


SYSTEM = Actor(email="system")


def _logs():
    return get_collection(Collections.AUDIT_LOGS)


def _clean(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        key: value
        for key, value in snapshot.items()
        if key not in _REDACTED_FIELDS and not isinstance(value, ObjectId)
    }


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Only what actually changed — a full snapshot pair buries the signal."""
    before, after = _clean(before) or {}, _clean(after) or {}
    changed = {}
    for key in set(before) | set(after):
        if key in {"updated_at", "created_at"}:
            continue
        if before.get(key) != after.get(key):
            changed[key] = {"from": before.get(key), "to": after.get(key)}
    return changed


async def record(
    actor: Actor,
    action: AuditAction,
    entity: AuditEntity,
    *,
    entity_id: str | None = None,
    entity_label: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    try:
        entry = AuditLog(
            actor_id=actor.id,
            actor_email=actor.email,
            action=action,
            entity=entity,
            entity_id=entity_id,
            entity_label=entity_label,
            before=_clean(before),
            after=_clean(after),
            ip=actor.ip,
        )
        await _logs().insert_one(entry.to_mongo())
    except Exception:  # noqa: BLE001 - history must never break the operation
        logger.exception(
            "Failed to write audit entry %s/%s for %s", action, entity, entity_id
        )


async def record_change(
    actor: Actor,
    action: AuditAction,
    entity: AuditEntity,
    *,
    entity_id: str | None = None,
    entity_label: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Record only the changed fields. Silent when nothing actually changed."""
    changes = diff(before, after)
    if not changes:
        return
    await record(
        actor,
        action,
        entity,
        entity_id=entity_id,
        entity_label=entity_label,
        before={key: value["from"] for key, value in changes.items()},
        after={key: value["to"] for key, value in changes.items()},
    )


async def list_logs(
    pagination: PaginationParams,
    *,
    actor_id: str | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    query: dict[str, Any] = {}
    if actor_id:
        query["actor_id"] = actor_id
    if entity:
        query["entity"] = entity
    if entity_id:
        query["entity_id"] = entity_id
    if action:
        query["action"] = action

    total = await _logs().count_documents(query)
    cursor = (
        _logs().find(query).sort("at", -1).skip(pagination.skip).limit(pagination.limit)
    )
    items = [serialize(document) for document in await cursor.to_list(length=pagination.limit)]
    return items, total
