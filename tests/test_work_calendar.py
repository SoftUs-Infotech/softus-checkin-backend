"""Which days the office is open: the Saturday pattern, overrides and swaps.

The first half needs no database — that is the point of keeping the decision
pure. The second half exercises the endpoints that store the rulings.

August 2026 is used wherever a fifth Saturday matters: it has five (1, 8, 15, 22
and 29). March 2026 is the ordinary month — four Saturdays, the 2nd on the 14th
and the 3rd on the 21st.
"""

from datetime import date

import pytest

from app.models.work_calendar import DayKind
from app.services.work_calendar import (
    SOURCE_OVERRIDE,
    SOURCE_SATURDAY_PATTERN,
    SOURCE_SHIFT,
    SOURCE_SWAP,
    count_working_days,
    decide_range,
    decide_working_day,
    nth_weekday_of_month,
    saturday_is_working,
)
from tests.conftest import authed_client, invite_and_accept

MON_TO_FRI = {"working_weekdays": [0, 1, 2, 3, 4]}
SIX_DAY_WEEK = {"working_weekdays": [0, 1, 2, 3, 4, 5]}

FIRST_SATURDAY = date(2026, 8, 1)
SECOND_SATURDAY = date(2026, 8, 8)
THIRD_SATURDAY = date(2026, 8, 15)
FOURTH_SATURDAY = date(2026, 8, 22)
FIFTH_SATURDAY = date(2026, 8, 29)

MARCH_SECOND_SATURDAY = date(2026, 3, 14)
MARCH_THIRD_SATURDAY = date(2026, 3, 21)


def decide(day: date, **kwargs):
    kwargs.setdefault("shift", MON_TO_FRI)
    kwargs.setdefault("working_saturdays", [2, 4])
    return decide_working_day(day, **kwargs)


# --------------------------------------------------------------------------- #
# the pattern
# --------------------------------------------------------------------------- #
def test_the_ordinal_counts_saturdays_within_the_month():
    assert nth_weekday_of_month(FIRST_SATURDAY) == 1
    assert nth_weekday_of_month(THIRD_SATURDAY) == 3
    assert nth_weekday_of_month(FIFTH_SATURDAY) == 5


def test_the_default_pattern_opens_the_second_and_fourth_saturday():
    assert decide(FIRST_SATURDAY).is_working is False
    assert decide(SECOND_SATURDAY).is_working is True
    assert decide(THIRD_SATURDAY).is_working is False
    assert decide(FOURTH_SATURDAY).is_working is True


def test_a_saturday_is_decided_by_the_organisation_not_the_shift():
    assert decide(SECOND_SATURDAY).source == SOURCE_SATURDAY_PATTERN


def test_a_shift_that_lists_saturday_still_follows_the_pattern():
    """The pattern is organisation policy and a shift cannot quietly opt out.

    It used to: a six-day shift worked every Saturday and the setting did
    nothing. Nearly every shift lists Saturday, so the pattern was dead.
    """
    assert decide(FIRST_SATURDAY, shift=SIX_DAY_WEEK).is_working is False
    assert decide(FIRST_SATURDAY, shift=SIX_DAY_WEEK).source == SOURCE_SATURDAY_PATTERN
    assert decide(SECOND_SATURDAY, shift=SIX_DAY_WEEK).is_working is True


def test_a_weekday_follows_the_shift():
    monday = date(2026, 8, 3)
    assert decide(monday).is_working is True
    assert decide(monday).source == SOURCE_SHIFT
    assert decide(date(2026, 8, 2)).is_working is False  # Sunday


# --------------------------------------------------------------------------- #
# the fifth Saturday
# --------------------------------------------------------------------------- #
def test_an_undecided_fifth_saturday_reads_as_off_and_says_so():
    decision = decide(FIFTH_SATURDAY, fifth_saturday_working=None)
    assert decision.is_working is False
    assert decision.is_undecided is True
    assert "fifth" in decision.reason.lower()


def test_a_decided_fifth_saturday_is_no_longer_undecided():
    assert decide(FIFTH_SATURDAY, fifth_saturday_working=True).is_working is True
    assert decide(FIFTH_SATURDAY, fifth_saturday_working=True).is_undecided is False
    assert decide(FIFTH_SATURDAY, fifth_saturday_working=False).is_working is False
    assert decide(FIFTH_SATURDAY, fifth_saturday_working=False).is_undecided is False


def test_the_pattern_list_may_carry_the_fifth_saturday_itself():
    working, undecided = saturday_is_working(
        FIFTH_SATURDAY, working_saturdays=[2, 4, 5], fifth_saturday_working=None
    )
    assert (working, undecided) == (True, False)


def test_a_four_saturday_month_never_asks_the_fifth_question():
    assert decide(MARCH_THIRD_SATURDAY, fifth_saturday_working=None).is_undecided is False


# --------------------------------------------------------------------------- #
# overrides beat everything
# --------------------------------------------------------------------------- #
def test_an_override_beats_the_saturday_pattern():
    decision = decide(
        SECOND_SATURDAY,
        override={"kind": DayKind.OFF, "reason": "Founders' day."},
    )
    assert decision.is_working is False
    assert decision.source == SOURCE_OVERRIDE
    assert decision.reason == "Founders' day."


def test_an_override_beats_the_shift_weekday():
    sunday = date(2026, 8, 2)
    decision = decide(sunday, override={"kind": DayKind.WORKING, "reason": "Go-live."})
    assert decision.is_working is True


def test_a_swap_half_is_reported_as_a_swap():
    decision = decide(
        MARCH_SECOND_SATURDAY,
        override={
            "kind": DayKind.OFF,
            "reason": "Swapped.",
            "swap_with_date": "2026-03-21",
        },
    )
    assert decision.source == SOURCE_SWAP
    assert decision.swap_with_date == "2026-03-21"


# --------------------------------------------------------------------------- #
# counting a month
# --------------------------------------------------------------------------- #
def test_march_has_twenty_four_working_days_under_the_default_pattern():
    decisions = decide_range(
        date(2026, 3, 1),
        date(2026, 3, 31),
        shift=MON_TO_FRI,
        working_saturdays=[2, 4],
    )
    assert count_working_days(decisions) == 24


def test_a_swap_within_a_month_leaves_the_count_alone():
    overrides = {
        "2026-03-14": {"kind": DayKind.OFF, "reason": "Swap", "swap_with_date": "2026-03-21"},
        "2026-03-21": {"kind": DayKind.WORKING, "reason": "Swap", "swap_with_date": "2026-03-14"},
    }
    decisions = decide_range(
        date(2026, 3, 1),
        date(2026, 3, 31),
        shift=MON_TO_FRI,
        working_saturdays=[2, 4],
        overrides=overrides,
    )
    assert count_working_days(decisions) == 24


# --------------------------------------------------------------------------- #
# the endpoints
# --------------------------------------------------------------------------- #
class TestApi:
    pytestmark = pytest.mark.usefixtures("db")

    async def test_an_employee_can_read_the_calendar(self, admin_client, api_prefix):
        session = await invite_and_accept(
            admin_client, api_prefix, email="calendar-reader@example.com"
        )
        client = authed_client(session["tokens"]["access_token"])
        async with client:
            response = await client.get(
                f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
            )
        assert response.status_code == 200
        assert response.json()["data"]["working_days"] == 24

    async def test_an_employee_cannot_change_the_calendar(self, admin_client, api_prefix):
        session = await invite_and_accept(
            admin_client, api_prefix, email="calendar-writer@example.com"
        )
        client = authed_client(session["tokens"]["access_token"])
        async with client:
            response = await client.post(
                f"{api_prefix}/work-calendar/overrides",
                json={"date": "2026-03-16", "kind": "OFF", "reason": "I would rather not."},
            )
        assert response.status_code == 403

    async def test_an_override_is_stored_and_wins(self, admin_client, api_prefix):
        response = await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={"date": "2026-03-16", "kind": "OFF", "reason": "Office fumigation."},
        )
        assert response.status_code == 201, response.text

        day = await admin_client.get(
            f"{api_prefix}/work-calendar/day", params={"on": "2026-03-16"}
        )
        assert day.json()["data"]["is_working"] is False
        assert day.json()["data"]["source"] == SOURCE_OVERRIDE

        month = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
        )
        assert month.json()["data"]["working_days"] == 23

    async def test_an_override_can_open_a_sunday(self, admin_client, api_prefix):
        await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={"date": "2026-03-15", "kind": "WORKING", "reason": "Release weekend."},
        )
        month = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
        )
        assert month.json()["data"]["working_days"] == 25

    async def test_a_swap_keeps_the_months_working_day_count(self, admin_client, api_prefix):
        response = await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-03-21",
                "reason": "Third Saturday off instead of the second.",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["working_days"]["2026-03"] == 24

        month = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
        )
        days = {item["date"]: item for item in month.json()["data"]["days"]}
        assert days["2026-03-14"]["is_working"] is False
        assert days["2026-03-21"]["is_working"] is True
        assert days["2026-03-21"]["swap_with_date"] == "2026-03-14"
        assert month.json()["data"]["working_days"] == 24

    async def test_a_swap_that_would_change_the_count_is_refused(
        self, admin_client, api_prefix
    ):
        """Both dates already worked, so opening one and closing the other loses a day."""
        response = await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-16",
                "working_date": "2026-03-17",
                "reason": "Two Mondays for the price of one.",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SWAP_CHANGES_WORKING_DAYS"
        assert "working days" in response.json()["message"]

    async def test_a_refused_swap_leaves_nothing_behind(self, admin_client, api_prefix):
        await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-16",
                "working_date": "2026-03-17",
                "reason": "Two Mondays for the price of one.",
            },
        )
        month = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
        )
        assert month.json()["data"]["working_days"] == 24

    async def test_a_cross_month_swap_is_refused(self, admin_client, api_prefix):
        response = await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-04-04",
                "reason": "Borrowing a day from April.",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SWAP_CHANGES_WORKING_DAYS"

    async def test_half_a_swap_cannot_be_deleted_on_its_own(self, admin_client, api_prefix):
        await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-03-21",
                "reason": "Third Saturday off instead of the second.",
            },
        )
        response = await admin_client.delete(
            f"{api_prefix}/work-calendar/overrides/2026-03-14"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "OVERRIDE_IS_SWAPPED"

    async def test_removing_a_swap_removes_both_halves(self, admin_client, api_prefix):
        await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-03-21",
                "reason": "Third Saturday off instead of the second.",
            },
        )
        response = await admin_client.delete(f"{api_prefix}/work-calendar/swaps/2026-03-21")
        assert response.status_code == 200
        assert sorted(response.json()["data"]["removed"]) == ["2026-03-14", "2026-03-21"]

        overrides = await admin_client.get(
            f"{api_prefix}/work-calendar/overrides",
            params={"from_date": "2026-03-01", "to_date": "2026-03-31"},
        )
        assert overrides.json()["data"] == []

    async def test_the_fifth_saturday_setting_reaches_the_calendar(
        self, admin_client, api_prefix
    ):
        before = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 8}
        )
        undecided = [
            item for item in before.json()["data"]["days"] if item["is_undecided"]
        ]
        assert [item["date"] for item in undecided] == ["2026-08-29"]

        await admin_client.patch(
            f"{api_prefix}/settings", json={"fifth_saturday_working": True}
        )
        after = await admin_client.get(
            f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 8}
        )
        days = {item["date"]: item for item in after.json()["data"]["days"]}
        assert days["2026-08-29"]["is_working"] is True
        assert days["2026-08-29"]["is_undecided"] is False
