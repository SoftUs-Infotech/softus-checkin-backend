from datetime import UTC, date, datetime

from app.core.datetime import (
    attendance_date,
    iter_dates,
    month_range,
    org_day_bounds,
    to_org_tz,
    to_utc,
)

# The org timezone in tests is Asia/Kolkata (UTC+05:30).


def test_naive_datetimes_are_treated_as_utc():
    assert to_utc(datetime(2026, 3, 1, 10, 0)).tzinfo == UTC


def test_late_evening_utc_belongs_to_the_next_org_day():
    # 19:00 UTC is 00:30 the following day in Kolkata.
    instant = datetime(2026, 3, 1, 19, 0, tzinfo=UTC)

    assert attendance_date(instant) == date(2026, 3, 2)
    assert to_org_tz(instant).hour == 0


def test_early_morning_utc_still_belongs_to_the_same_org_day():
    instant = datetime(2026, 3, 1, 4, 0, tzinfo=UTC)

    assert attendance_date(instant) == date(2026, 3, 1)


def test_org_day_bounds_span_exactly_24_hours_in_utc():
    start, end = org_day_bounds(date(2026, 3, 1))

    assert start == datetime(2026, 2, 28, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 3, 1, 18, 30, tzinfo=UTC)


def test_month_range_handles_december_rollover():
    assert month_range(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))


def test_month_range_handles_a_leap_february():
    assert month_range(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))


def test_iter_dates_is_inclusive_of_both_ends():
    days = list(iter_dates(date(2026, 3, 1), date(2026, 3, 4)))

    assert days == [date(2026, 3, i) for i in range(1, 5)]
