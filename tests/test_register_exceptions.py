"""What counts as something an administrator has to chase.

Both of these came out of the seeded demo data: a Saturday the office had closed
was listed as needing attention because somebody had worked it "late", and a
Wednesday closed by hand was labelled a weekly off.
"""

import pytest

from app.models.attendance import AttendanceStatus
from app.services.attendance import _needs_attention, _status_label

# 2026-08-19 is a Wednesday; 2026-08-22 a Saturday.
WEDNESDAY = "2026-08-19"
SATURDAY = "2026-08-22"


def day(status: AttendanceStatus, **extra) -> dict:
    return {"date": WEDNESDAY, "status": status.value, "sessions": [], **extra}


def test_an_absence_needs_attention():
    assert _needs_attention(day(AttendanceStatus.ABSENT)) is True


def test_a_forgotten_punch_out_needs_attention():
    assert _needs_attention(day(AttendanceStatus.MISSING_PUNCH_OUT)) is True


def test_a_late_arrival_on_a_working_day_needs_attention():
    assert _needs_attention(day(AttendanceStatus.PRESENT, is_late=True)) is True


def test_lateness_on_a_day_the_office_was_closed_does_not():
    """There was no shift to be late for; they earned a comp-off instead."""
    assert _needs_attention(day(AttendanceStatus.WEEKEND, is_late=True)) is False


def test_a_holiday_worked_from_a_phone_does_not_need_attention():
    record = day(
        AttendanceStatus.HOLIDAY,
        sessions=[{"punch_in": {"device_class": "MOBILE"}}],
    )

    assert _needs_attention(record) is False


def test_a_phone_punch_on_a_working_day_still_needs_attention():
    record = day(
        AttendanceStatus.PRESENT,
        sessions=[{"punch_in": {"device_class": "MOBILE"}}],
    )

    assert _needs_attention(record) is True


def test_a_punch_away_from_the_office_needs_attention():
    record = day(
        AttendanceStatus.PRESENT,
        sessions=[{"punch_in": {"outside_office": True}}],
    )

    assert _needs_attention(record) is True


def test_an_ordinary_present_day_does_not():
    assert _needs_attention(day(AttendanceStatus.PRESENT)) is False


def test_a_closed_weekday_is_not_called_a_weekly_off():
    assert _status_label(AttendanceStatus.WEEKEND, WEDNESDAY) == "Office closed"


def test_a_saturday_off_is_still_a_weekly_off():
    assert _status_label(AttendanceStatus.WEEKEND, SATURDAY) == "Weekly off"


@pytest.mark.parametrize(
    "status", [AttendanceStatus.PRESENT, AttendanceStatus.ABSENT, AttendanceStatus.HOLIDAY]
)
def test_every_other_status_keeps_its_own_label(status):
    assert _status_label(status, WEDNESDAY) not in ("Office closed", "")
