"""Time helpers.

Everything is stored in UTC. The *attendance date* an employee sees is derived
in the organisation's timezone — without this, an evening punch-out lands on the
wrong calendar day.
"""

from datetime import UTC, date, datetime, time, timedelta

from app.core.config import settings


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalise any datetime to aware UTC, assuming naive values are already UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_org_tz(value: datetime) -> datetime:
    """Render a UTC instant in the organisation's timezone."""
    return to_utc(value).astimezone(settings.org_tz)


def org_today() -> date:
    """Today's date in the organisation's timezone."""
    return to_org_tz(utc_now()).date()


def attendance_date(value: datetime) -> date:
    """The attendance day a given instant belongs to."""
    return to_org_tz(value).date()


def date_key(value: date) -> str:
    """Canonical ``YYYY-MM-DD`` key used for attendance/holiday documents."""
    return value.isoformat()


def parse_date_key(value: str) -> date:
    return date.fromisoformat(value)


def org_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` instants covering a given organisation-local day."""
    tz = settings.org_tz
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def month_range(year: int, month: int) -> tuple[date, date]:
    """Inclusive first and last date of a month."""
    first = date(year, month, 1)
    last = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, last - timedelta(days=1)


def iter_dates(start: date, end: date):
    """Yield every date from ``start`` to ``end`` inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
