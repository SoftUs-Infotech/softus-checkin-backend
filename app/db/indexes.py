"""Index declarations, applied once on startup.

Declared as data so the full set is visible in one place. ``create_indexes`` is
idempotent — MongoDB ignores an index that already exists with the same spec.
"""

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.core.logging import get_logger
from app.db.mongo import Collections, get_database

logger = get_logger(__name__)

INDEXES: dict[str, list[IndexModel]] = {
    Collections.USERS: [
        IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
        IndexModel(
            [("employee_code", ASCENDING)],
            unique=True,
            sparse=True,
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
}


async def create_indexes() -> None:
    database = get_database()
    for collection_name, models in INDEXES.items():
        if not models:
            continue
        await database[collection_name].create_indexes(models)
        logger.debug("Ensured %d index(es) on '%s'.", len(models), collection_name)
    logger.info("Indexes ensured for %d collection(s).", len(INDEXES))


__all__ = ["INDEXES", "create_indexes", "ASCENDING", "DESCENDING", "IndexModel"]
