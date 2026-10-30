"""Which days the office is open: the Saturday pattern, overrides and swaps.

The first half needs no database — that is the point of keeping the decision
pure. The second half exercises the endpoints that store the rulings.

August 2026 is used wherever a fifth Saturday matters: it has five (1, 8, 15, 22
and 29). March 2026 is the ordinary month — four Saturdays, the 2nd on the 14th
and the 3rd on the 21st.
"""

from datetime import date

import pytest

MON_TO_FRI = {"working_weekdays": [0, 1, 2, 3, 4]}
SIX_DAY_WEEK = {"working_weekdays": [0, 1, 2, 3, 4, 5]}

FIRST_SATURDAY = date(2026, 8, 1)
SECOND_SATURDAY = date(2026, 8, 8)
THIRD_SATURDAY = date(2026, 8, 15)
FOURTH_SATURDAY = date(2026, 8, 22)
FIFTH_SATURDAY = date(2026, 8, 29)

MARCH_SECOND_SATURDAY = date(2026, 3, 14)
MARCH_THIRD_SATURDAY = date(2026, 3, 21)


# --------------------------------------------------------------------------- #
# the endpoints
# --------------------------------------------------------------------------- #
class TestApi:
    pytestmark = pytest.mark.usefixtures("db")


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
