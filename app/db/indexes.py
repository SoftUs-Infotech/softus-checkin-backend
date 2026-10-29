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
        # Two-factor verification looks an account up by its pending challenge.
        # Partial, because only a handful of accounts hold one at any moment.
        IndexModel(
            [("two_factor.challenge.token_hash", ASCENDING)],
            partialFilterExpression={"two_factor.challenge.token_hash": {"$type": "string"}},
            name="two_factor_challenge",
        ),
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
    Collections.WORK_CALENDAR: [
        # One ruling per date: working or off.
        IndexModel([("date", ASCENDING)], unique=True, name="uniq_work_calendar_date"),
        IndexModel([("swap_with_date", ASCENDING)], name="work_calendar_swap"),
    ],
    Collections.FULL_DAY_CREDITS: [
        IndexModel([("date", ASCENDING)], unique=True, name="uniq_full_day_date"),
    ],
    Collections.COMP_OFFS: [
        IndexModel([("user_id", ASCENDING), ("status", ASCENDING)], name="comp_off_user_status"),
        IndexModel([("expires_on", ASCENDING)], name="comp_off_expiry"),
    ],
    Collections.SALARIES: [
        # A history, not a field on the user: a raise must not erase what the
        # employee was paid before it.
        IndexModel(
            [("user_id", ASCENDING), ("effective_from", DESCENDING)],
            unique=True,
            name="uniq_salary_user_effective",
        ),
    ],
    Collections.SALARY_ADJUSTMENTS: [
        IndexModel(
            [("user_id", ASCENDING), ("apply_month", ASCENDING)],
            name="salary_adjustment_user_month",
        ),
        IndexModel([("source_date", ASCENDING)], name="salary_adjustment_source"),
    ],
    Collections.GEOCODE_CACHE: [
        IndexModel([("key", ASCENDING)], unique=True, name="uniq_geocode_key"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="geocode_ttl"),
    ],
    Collections.ATTENDANCE: [
        # One document per employee per day. This is the guard that makes a
        # double submission impossible rather than merely unlikely.
        IndexModel(
            [("user_id", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uniq_attendance_user_day",
        ),
        IndexModel([("date", ASCENDING), ("status", ASCENDING)], name="attendance_day_status"),
        IndexModel([("user_id", ASCENDING), ("date", DESCENDING)], name="attendance_history"),
    ],
    Collections.PUNCH_REQUESTS: [
        IndexModel(
            [("user_id", ASCENDING), ("request_key", ASCENDING)],
            unique=True,
            name="uniq_punch_request",
        ),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="punch_request_ttl"),
    ],
    Collections.CORRECTIONS: [
        IndexModel([("user_id", ASCENDING), ("date", ASCENDING)], name="correction_user_day"),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="correction_queue"),
    ],
    Collections.TASKS: [
        IndexModel([("user_id", ASCENDING), ("date", DESCENDING)], name="task_user_day"),
        IndexModel([("status", ASCENDING), ("date", DESCENDING)], name="task_queue"),
        # Carry-forward asks "what was still open on this day", which is a scan
        # of unfinished work per employee unless completion is indexed too.
        IndexModel(
            [("user_id", ASCENDING), ("completed_on", ASCENDING)],
            name="task_user_completion",
        ),
    ],
    Collections.LEAVE_REQUESTS: [
        IndexModel([("user_id", ASCENDING), ("status", ASCENDING)], name="leave_user_status"),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="leave_queue"),
        IndexModel([("days.date", ASCENDING), ("status", ASCENDING)], name="leave_by_day"),
    ],
    Collections.LEAVE_BALANCES: [
        IndexModel(
            [("user_id", ASCENDING), ("leave_type_id", ASCENDING), ("year", ASCENDING)],
            unique=True,
            name="uniq_leave_balance",
        ),
    ],
    Collections.ANNOUNCEMENTS: [
        IndexModel(
            [("is_published", DESCENDING), ("pinned", DESCENDING), ("created_at", DESCENDING)],
            name="announcement_feed",
        ),
        IndexModel([("type", ASCENDING), ("event_date", ASCENDING)], name="announcement_events"),
    ],
    Collections.NOTIFICATIONS: [
        IndexModel(
            [("user_id", ASCENDING), ("read_at", ASCENDING), ("created_at", DESCENDING)],
            name="notification_inbox",
        ),
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
