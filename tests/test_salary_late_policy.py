"""Lateness with a consequence.

Recording that somebody was late is easy; the policy is what turns it into a
number, and it has to be the policy an administrator thinks they enabled.
"""

import pytest

from app.services.salary import _late_penalty_days

OFF = {"late_arrivals_per_half_day": 0}
EVERY_THIRD = {"late_arrivals_per_half_day": 3}


@pytest.mark.parametrize(
    ("late_days", "expected"),
    [(2, 0.0), (6, 1.0)],  # two cost nothing; six cost a whole day
)
def test_every_third_late_arrival_costs_half_a_day(late_days, expected):
    assert _late_penalty_days(late_days, EVERY_THIRD) == expected
