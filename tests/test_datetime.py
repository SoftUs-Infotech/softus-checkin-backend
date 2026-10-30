from datetime import UTC, date, datetime

from app.core.datetime import (
    attendance_date,
    to_org_tz,
)

# The org timezone in tests is Asia/Kolkata (UTC+05:30).


def test_late_evening_utc_belongs_to_the_next_org_day():
    # 19:00 UTC is 00:30 the following day in Kolkata.
    instant = datetime(2026, 3, 1, 19, 0, tzinfo=UTC)

    assert attendance_date(instant) == date(2026, 3, 2)
    assert to_org_tz(instant).hour == 0
