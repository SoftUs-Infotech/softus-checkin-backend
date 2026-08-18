"""Motor client lifecycle and collection access.

The client is created once on startup and closed on shutdown. Nothing else in the
application constructs a client; services ask for collections by name.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class Collections:
    """Canonical collection names — never type these as string literals elsewhere."""

    USERS = "users"
    INVITES = "invites"
    REFRESH_TOKENS = "refresh_tokens"
    PASSWORD_RESETS = "password_resets"
    SHIFTS = "shifts"
    ATTENDANCE = "attendance"
    CORRECTIONS = "corrections"
    TASKS = "tasks"
    LEAVE_TYPES = "leave_types"
    LEAVE_REQUESTS = "leave_requests"
    LEAVE_BALANCES = "leave_balances"
    HOLIDAYS = "holidays"
    ANNOUNCEMENTS = "announcements"
    NOTIFICATIONS = "notifications"
    AUDIT_LOGS = "audit_logs"
    SETTINGS = "settings"


class _MongoState:
    client: AsyncIOMotorClient | None = None
    database: AsyncIOMotorDatabase | None = None


_state = _MongoState()


async def connect_to_mongo() -> None:
    if _state.client is not None:
        return

    logger.info("Connecting to MongoDB at %s (db=%s)", settings.MONGO_URI, settings.MONGO_DB)
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        settings.MONGO_URI,
        serverSelectionTimeoutMS=5000,
        uuidRepresentation="standard",
    )
    await client.admin.command("ping")

    _state.client = client
    _state.database = client[settings.MONGO_DB]
    logger.info("MongoDB connection established.")


async def close_mongo_connection() -> None:
    if _state.client is None:
        return
    _state.client.close()
    _state.client = None
    _state.database = None
    logger.info("MongoDB connection closed.")


def get_database() -> AsyncIOMotorDatabase:
    if _state.database is None:
        raise ServiceUnavailableError("The database connection is not available.")
    return _state.database


def get_collection(name: str) -> AsyncIOMotorCollection:
    return get_database()[name]


async def ping() -> bool:
    """True when the database answers. Used by the readiness probe."""
    if _state.client is None:
        return False
    try:
        await _state.client.admin.command("ping")
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("MongoDB ping failed.", exc_info=True)
        return False
    return True
