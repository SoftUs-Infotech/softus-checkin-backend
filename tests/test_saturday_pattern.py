"""The Saturday pattern is organisation policy and nothing quietly overrules it.

It used to defer to any shift that listed Saturday among its working days.
Nearly every shift does, so "the 2nd and 4th are working" never took effect for
anybody — the setting existed and did nothing.
"""

from datetime import date

import pytest

from app.services.work_calendar import count_working_days, decide_range, decide_working_day

# August 2026 has five Saturdays — 1, 8, 15, 22, 29 — and five Sundays.
AUGUST_FIRST = date(2026, 8, 1)
AUGUST_LAST = date(2026, 8, 31)

SIX_DAY_SHIFT = {"working_weekdays": [0, 1, 2, 3, 4, 5]}
FIVE_DAY_SHIFT = {"working_weekdays": [0, 1, 2, 3, 4]}


def saturdays(shift):
    days = decide_range(
        AUGUST_FIRST,
        AUGUST_LAST,
        shift=shift,
        working_saturdays=[2, 4],
        fifth_saturday_working=None,
        overrides={},
    )
    return {day.date: day.is_working for day in days if day.day.weekday() == 5}


def test_a_shift_that_lists_saturday_no_longer_kills_the_pattern():
    assert saturdays(SIX_DAY_SHIFT) == {
        "2026-08-01": False,
        "2026-08-08": True,
        "2026-08-15": False,
        "2026-08-22": True,
        "2026-08-29": False,
    }


def test_a_five_day_shift_reads_the_same_pattern():
    assert saturdays(FIVE_DAY_SHIFT) == saturdays(SIX_DAY_SHIFT)


def test_the_pattern_is_the_stated_source():
    decision = decide_working_day(
        date(2026, 8, 8), shift=SIX_DAY_SHIFT, working_saturdays=[2, 4]
    )

    assert decision.source == "SATURDAY_PATTERN"
    assert decision.is_working is True


def test_a_per_date_ruling_still_beats_the_pattern():
    decision = decide_working_day(
        date(2026, 8, 8),
        shift=SIX_DAY_SHIFT,
        working_saturdays=[2, 4],
        override={"kind": "OFF", "reason": "Diwali week"},
    )

    assert decision.is_working is False


def test_the_month_counts_the_days_the_office_is_actually_open():
    """Your example: 31 days, 5 Sundays, 3 Saturdays off — 23 working days."""
    days = decide_range(
        AUGUST_FIRST,
        AUGUST_LAST,
        shift=SIX_DAY_SHIFT,
        working_saturdays=[2, 4],
        fifth_saturday_working=None,
        overrides={},
    )

    assert count_working_days(days) == 23


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [([1, 2, 3, 4, 5], 26), ([2, 4], 23), ([], 21)],
)
def test_changing_the_pattern_changes_the_working_day_count(pattern, expected):
    days = decide_range(
        AUGUST_FIRST,
        AUGUST_LAST,
        shift=SIX_DAY_SHIFT,
        working_saturdays=pattern,
        fifth_saturday_working=None,
        overrides={},
    )

    assert count_working_days(days) == expected


@pytest.mark.asyncio
async def test_a_settings_document_written_before_the_field_existed_keeps_the_default(db):
    """The demo database had no value stored, and read as "every Saturday off"."""
    from app.db.mongo import Collections
    from app.models.settings import SETTINGS_KEY
    from app.services import settings as settings_service
    from app.services.work_calendar import _pattern

    await db[Collections.SETTINGS].insert_one({"key": SETTINGS_KEY, "org_name": "Legacy"})
    settings_service.clear_cache()

    saturdays, fifth = await _pattern()

    assert saturdays == [2, 4]
    assert fifth is None


@pytest.mark.asyncio
async def test_an_empty_list_is_respected_as_a_real_choice(db):
    from app.db.mongo import Collections
    from app.models.settings import SETTINGS_KEY
    from app.services import settings as settings_service
    from app.services.work_calendar import _pattern

    await db[Collections.SETTINGS].insert_one(
        {"key": SETTINGS_KEY, "working_saturdays": []}
    )
    settings_service.clear_cache()

    saturdays, _ = await _pattern()

    assert saturdays == []
