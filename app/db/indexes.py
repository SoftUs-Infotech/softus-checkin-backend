"""Index declarations, applied once on startup.

Declared as data so the full set is visible in one place. ``create_indexes`` is
idempotent — MongoDB ignores an index that already exists with the same spec.
"""

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import OperationFailure

from app.core.logging import get_logger
from app.db.mongo import Collections, get_database

logger = get_logger(__name__)

INDEXES: dict[str, list[IndexModel]] = {
    Collections.USERS: [
        IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
        # A partial filter, not `sparse`: invited employees have the field
        # present but null, and `sparse` would still treat every null as a
        # collision.
        IndexModel(
            [("employee_code", ASCENDING)],
            unique=True,
            partialFilterExpression={"employee_code": {"$type": "string"}},
            name="uniq_employee_code",
        ),
        IndexModel([("role", ASCENDING), ("status", ASCENDING)], name="role_status"),
        IndexModel([("reporting_manager_id", ASCENDING)], name="reporting_manager"),
    ],
    Collections.INVITES: [
        IndexModel([("token_hash", ASCENDING)], unique=True, name="uniq_invite_token"),
        IndexModel([("email", ASCENDING), ("status", ASCENDING)], name="email_status"),
        IndexModel([("expires_at", ASCENDING)], name="invite_expiry"),
    ],
    Collections.REFRESH_TOKENS: [
        IndexModel([("token_hash", ASCENDING)], unique=True, name="uniq_refresh_token"),
        IndexModel([("user_id", ASCENDING)], name="refresh_user"),
        IndexModel([("family_id", ASCENDING)], name="refresh_family"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="refresh_ttl"),
    ],
    Collections.PASSWORD_RESETS: [
        IndexModel([("token_hash", ASCENDING)], unique=True, name="uniq_reset_token"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="reset_ttl"),
    ],
    Collections.SHIFTS: [
        IndexModel(
            [("code", ASCENDING)],
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="uniq_shift_code",
        ),
        IndexModel([("is_default", DESCENDING), ("is_active", DESCENDING)], name="shift_default"),
    ],
    Collections.HOLIDAYS: [
        IndexModel(
            [("date", ASCENDING), ("name", ASCENDING)],
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="uniq_holiday_date_name",
        ),
        IndexModel([("year", ASCENDING), ("date", ASCENDING)], name="holiday_year"),
    ],
    Collections.LEAVE_TYPES: [
        IndexModel(
            [("code", ASCENDING)],
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="uniq_leave_type_code",
        ),
        IndexModel([("sort_order", ASCENDING), ("name", ASCENDING)], name="leave_type_order"),
    ],
    Collections.SETTINGS: [
        IndexModel([("key", ASCENDING)], unique=True, name="uniq_settings_key"),
    ],
    Collections.AUDIT_LOGS: [
        IndexModel([("at", DESCENDING)], name="audit_recent"),
        IndexModel([("entity", ASCENDING), ("entity_id", ASCENDING)], name="audit_entity"),
        IndexModel([("actor_id", ASCENDING), ("at", DESCENDING)], name="audit_actor"),
    ],
}


_INDEX_SPECS_CONFLICT = 86


async def create_indexes() -> None:
    """Ensure every declared index exists, rebuilding any whose spec has changed.

    MongoDB refuses to redefine an index under an existing name. Without this
    reconciliation, editing a spec here would leave the old index in place in
    every environment that already ran the previous version.
    """
    database = get_database()
    for collection_name, models in INDEXES.items():
        collection = database[collection_name]
        for model in models:
            name = model.document["name"]
            try:
                await collection.create_indexes([model])
            except OperationFailure as exc:
                if exc.code != _INDEX_SPECS_CONFLICT:
                    raise
                logger.warning(
                    "Index '%s' on '%s' changed; rebuilding it.", name, collection_name
                )
                await collection.drop_index(name)
                await collection.create_indexes([model])
    logger.info("Indexes ensured for %d collection(s).", len(INDEXES))


__all__ = ["INDEXES", "create_indexes", "ASCENDING", "DESCENDING", "IndexModel"]
