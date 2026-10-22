"""Lateness with a consequence.

Recording that somebody was late is easy; the policy is what turns it into a
number, and it has to be the policy an administrator thinks they enabled.
"""

import pytest

from app.services.salary import DayCounts, _late_penalty_days, deduction_lines

OFF = {"late_arrivals_per_half_day": 0}
EVERY_THIRD = {"late_arrivals_per_half_day": 3}


@pytest.mark.parametrize("late_days", [0, 1, 5, 31])
def test_the_policy_switched_off_never_costs_anything(late_days):
    assert _late_penalty_days(late_days, OFF) == 0.0


@pytest.mark.parametrize(
    ("late_days", "expected"),
    [(0, 0.0), (1, 0.0), (2, 0.0), (3, 0.5), (5, 0.5), (6, 1.0), (9, 1.5)],
)
def test_every_third_late_arrival_costs_half_a_day(late_days, expected):
    assert _late_penalty_days(late_days, EVERY_THIRD) == expected


def test_a_missing_setting_is_read_as_switched_off():
    assert _late_penalty_days(10, {}) == 0.0


def test_the_deduction_line_prices_the_penalty_and_says_how_it_was_reached():
    counts = DayCounts(working_days=22, present_days=22, late_days=6)

    lines = deduction_lines(counts, day_rate=1000, flags=EVERY_THIRD)

    late = next(line for line in lines if line["kind"] == "LATE_ARRIVALS")
    assert late["days"] == 1.0
    assert late["amount"] == 1000
    assert "6 at 3" in late["label"]


def test_no_line_appears_when_the_threshold_has_not_been_reached():
    counts = DayCounts(working_days=22, present_days=22, late_days=2)

    lines = deduction_lines(counts, day_rate=1000, flags=EVERY_THIRD)

    assert not [line for line in lines if line["kind"] == "LATE_ARRIVALS"]
