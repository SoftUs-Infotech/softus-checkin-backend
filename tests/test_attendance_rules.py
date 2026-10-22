"""The day-status rules — every branch, no database.

Times are given in the organisation timezone (Asia/Kolkata, UTC+05:30) and
converted, because that is how they arrive in real life.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.attendance import AttendanceStatus
from app.services.attendance_rules import (
    apply_break_deduction,
    compute_day_status,
    expected_minutes_for,
    format_minutes,
    is_late_arrival,
    summarise_sessions,
)

SHIFT = {
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
    "grace_minutes": 15,
    "break_minutes": 0,
    "working_weekdays": [0, 1, 2, 3, 4],
}

MONDAY = date(2026, 3, 2)
SATURDAY = date(2026, 3, 7)
TODAY = date(2026, 3, 10)  # a Tuesday
NOW = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)  # 13:30 local


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in Asia/Kolkata expressed as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(
        hours=5, minutes=30
    )


def session(day: date, start: tuple[int, int], end: tuple[int, int] | None):
    entry = {"punch_in": {"at": local(day, *start)}}
    entry["punch_out"] = {"at": local(day, *end)} if end else None
    return entry


def compute(day, sessions, shift=SHIFT, **kwargs):
    return compute_day_status(
        day, sessions, shift, today=kwargs.pop("today", TODAY), now=kwargs.pop("now", NOW), **kwargs
    )


# --------------------------------------------------------------------------- #
# summing sessions
# --------------------------------------------------------------------------- #
def test_a_completed_session_is_measured_to_the_minute():
    worked, is_open, first_in, last_out = summarise_sessions(
        [session(MONDAY, (9, 30), (18, 30))], now=NOW
    )

    assert worked == 540
    assert is_open is False
    assert first_in is not None and last_out is not None


def test_several_sessions_add_up():
    worked, _, _, _ = summarise_sessions(
        [session(MONDAY, (9, 30), (13, 0)), session(MONDAY, (14, 0), (18, 30))], now=NOW
    )

    assert worked == 210 + 270


def test_an_open_session_counts_up_to_now_while_the_day_is_live():
    worked, is_open, _, _ = summarise_sessions(
        [session(TODAY, (9, 30), None)],
        now=local(TODAY, 12, 30),
        count_open_until=local(TODAY, 12, 30),
    )

    assert worked == 180
    assert is_open is True


def test_an_open_session_on_a_past_day_is_worth_nothing():
    """Otherwise one forgotten punch-out silently becomes hundreds of hours."""
    worked, is_open, _, _ = summarise_sessions(
        [session(MONDAY, (9, 30), None)], now=NOW, count_open_until=None
    )

    assert worked == 0
    assert is_open is True


def test_a_completed_session_still_counts_on_a_past_day():
    worked, _, _, _ = summarise_sessions(
        [session(MONDAY, (9, 30), (13, 30)), session(MONDAY, (14, 0), None)],
        now=NOW,
        count_open_until=None,
    )

    assert worked == 240


def test_a_session_with_no_punch_in_is_ignored():
    worked, _, _, _ = summarise_sessions([{"punch_out": {"at": local(MONDAY, 18, 0)}}], now=NOW)

    assert worked == 0


def test_a_reversed_interval_contributes_nothing_rather_than_subtracting():
    worked, _, _, _ = summarise_sessions(
        [{"punch_in": {"at": local(MONDAY, 18, 0)}, "punch_out": {"at": local(MONDAY, 9, 0)}}],
        now=NOW,
    )

    assert worked == 0


def test_no_sessions_at_all():
    assert summarise_sessions([], now=NOW) == (0, False, None, None)


# --------------------------------------------------------------------------- #
# break deduction
# --------------------------------------------------------------------------- #
def test_a_break_is_deducted_from_one_continuous_session():
    assert apply_break_deduction(600, session_count=1, break_minutes=60) == 540


def test_a_break_is_not_deducted_twice_when_the_employee_punched_out_for_lunch():
    assert apply_break_deduction(540, session_count=2, break_minutes=60) == 540


def test_a_break_cannot_push_worked_time_below_zero():
    assert apply_break_deduction(30, session_count=1, break_minutes=60) == 0


# --------------------------------------------------------------------------- #
# lateness
# --------------------------------------------------------------------------- #
def test_arriving_within_the_grace_period_is_not_late():
    assert is_late_arrival(local(MONDAY, 9, 44), SHIFT) is False


def test_arriving_after_the_grace_period_is_late():
    assert is_late_arrival(local(MONDAY, 9, 46), SHIFT) is True


def test_lateness_is_unknown_without_a_shift_or_a_punch():
    assert is_late_arrival(None, SHIFT) is False
    assert is_late_arrival(local(MONDAY, 11, 0), None) is False


def test_a_malformed_shift_start_does_not_crash_lateness():
    assert is_late_arrival(local(MONDAY, 11, 0), {"start_time": "nonsense"}) is False


# --------------------------------------------------------------------------- #
# statuses
# --------------------------------------------------------------------------- #
def test_a_full_day_is_present():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (18, 30))])

    assert result.status is AttendanceStatus.PRESENT
    assert result.worked_minutes == 540
    assert result.shortfall_minutes == 0


def test_exactly_meeting_the_requirement_counts_as_present():
    result = compute(MONDAY, [session(MONDAY, (10, 0), (19, 0))])

    assert result.status is AttendanceStatus.PRESENT


def test_four_hours_is_a_half_day():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (13, 30))])

    assert result.status is AttendanceStatus.HALF_DAY
    assert result.shortfall_minutes == 300


def test_under_the_half_day_threshold_is_short_hours():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (11, 0))])

    assert result.status is AttendanceStatus.SHORT_HOURS


def test_a_past_day_with_no_punch_at_all_is_absent():
    result = compute(MONDAY, [])

    assert result.status is AttendanceStatus.ABSENT


def test_a_past_day_left_open_is_a_missing_punch_out_worth_no_hours():
    result = compute(MONDAY, [session(MONDAY, (9, 30), None)])

    assert result.status is AttendanceStatus.MISSING_PUNCH_OUT
    # The employee forgot to punch out; nobody knows when they left.
    assert result.worked_minutes == 0


def test_a_forgotten_punch_out_does_not_inflate_the_earlier_sessions():
    result = compute(
        MONDAY,
        [session(MONDAY, (9, 30), (13, 30)), session(MONDAY, (14, 15), None)],
    )

    assert result.status is AttendanceStatus.MISSING_PUNCH_OUT
    assert result.worked_minutes == 240


def test_today_left_open_is_still_in_progress():
    result = compute(TODAY, [session(TODAY, (9, 30), None)], now=local(TODAY, 13, 30))

    assert result.status is AttendanceStatus.IN_PROGRESS
    assert result.worked_minutes == 240


def test_today_before_the_first_punch_is_not_started():
    result = compute(TODAY, [])

    assert result.status is AttendanceStatus.NOT_STARTED


def test_a_future_working_day_is_upcoming():
    result = compute(date(2026, 4, 1), [])  # a Wednesday

    assert result.status is AttendanceStatus.UPCOMING


def test_leave_booked_ahead_is_visible_on_a_future_day():
    """An employee planning next month must see the leave they already booked."""
    result = compute(date(2026, 4, 1), [], leave={"day_part": "FULL"})

    assert result.status is AttendanceStatus.ON_LEAVE


def test_a_holiday_is_visible_on_a_future_day():
    result = compute(date(2026, 4, 1), [], holiday={"name": "Good Friday"})

    assert result.status is AttendanceStatus.HOLIDAY


def test_a_future_weekend_is_still_a_weekly_off():
    result = compute(date(2026, 4, 4), [])  # a Saturday

    assert result.status is AttendanceStatus.WEEKEND


def test_a_non_working_weekday_is_a_weekly_off():
    result = compute(SATURDAY, [])

    assert result.status is AttendanceStatus.WEEKEND


def test_a_saturday_shift_makes_saturday_a_working_day():
    six_day = {**SHIFT, "working_weekdays": [0, 1, 2, 3, 4, 5]}

    result = compute(SATURDAY, [], shift=six_day)

    assert result.status is AttendanceStatus.ABSENT


# --------------------------------------------------------------------------- #
# precedence
# --------------------------------------------------------------------------- #
def test_a_holiday_outranks_everything_else():
    result = compute(MONDAY, [], holiday={"name": "Holi"})

    assert result.status is AttendanceStatus.HOLIDAY


def test_working_on_a_holiday_still_records_the_hours():
    result = compute(
        MONDAY, [session(MONDAY, (9, 30), (18, 30))], holiday={"name": "Holi"}
    )

    assert result.status is AttendanceStatus.HOLIDAY
    assert result.worked_minutes == 540


def test_approved_full_day_leave_excuses_the_day():
    result = compute(MONDAY, [], leave={"day_part": "FULL"})

    assert result.status is AttendanceStatus.ON_LEAVE


def test_a_holiday_outranks_leave():
    result = compute(MONDAY, [], holiday={"name": "Holi"}, leave={"day_part": "FULL"})

    assert result.status is AttendanceStatus.HOLIDAY


def test_half_day_leave_halves_what_the_day_requires():
    result = compute(
        MONDAY, [session(MONDAY, (14, 0), (18, 30))], leave={"day_part": "FIRST_HALF"}
    )

    assert result.expected_minutes == 240
    assert result.worked_minutes == 270
    assert result.status is AttendanceStatus.PRESENT


def test_half_day_leave_with_too_few_hours_is_still_short():
    result = compute(
        MONDAY, [session(MONDAY, (16, 0), (18, 0))], leave={"day_part": "SECOND_HALF"}
    )

    assert result.status is AttendanceStatus.SHORT_HOURS


def test_a_missing_punch_out_beats_a_weekend_only_when_the_day_is_worked():
    """A weekly off stays a weekly off, open session or not."""
    result = compute(SATURDAY, [session(SATURDAY, (9, 30), None)])

    assert result.status is AttendanceStatus.WEEKEND


# --------------------------------------------------------------------------- #
# defaults and formatting
# --------------------------------------------------------------------------- #
def test_a_missing_shift_falls_back_to_nine_hours_monday_to_friday():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (18, 30))], shift=None)

    assert result.expected_minutes == 540
    assert result.status is AttendanceStatus.PRESENT


def test_expected_minutes_never_exceed_the_full_day_requirement():
    short_shift = {**SHIFT, "required_minutes": 200, "half_day_minutes": 240}

    assert expected_minutes_for(short_shift, "FIRST_HALF") == 200


def test_a_night_shift_spanning_midnight_is_measured_correctly():
    night = {**SHIFT, "start_time": "22:00", "end_time": "06:00", "required_minutes": 480}
    sessions = [
        {
            "punch_in": {"at": local(MONDAY, 22, 0)},
            "punch_out": {"at": local(MONDAY, 22, 0) + timedelta(hours=8)},
        }
    ]

    result = compute(MONDAY, sessions, shift=night)

    assert result.worked_minutes == 480
    assert result.status is AttendanceStatus.PRESENT


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "0m"), (45, "45m"), (60, "1h"), (545, "9h 5m"), (-10, "0m")],
)
def test_minutes_are_formatted_for_display(minutes, expected):
    assert format_minutes(minutes) == expected
