"""Carry-forward: one row that keeps appearing until somebody closes it."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = org_today()
FIVE_DAYS_AGO = date_key(TODAY - timedelta(days=5))
TOMORROW = date_key(TODAY + timedelta(days=1))


async def _employee(admin_client, api_prefix, email):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session["user"]["id"]


async def _log_task(client, api_prefix, *, title, date):
    response = await client.post(
        f"{api_prefix}/tasks", json={"date": date, "title": title}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _day(client, api_prefix, day, **params):
    response = await client.get(f"{api_prefix}/tasks/day", params={"date": day, **params})
    assert response.status_code == 200, response.text
    return {item["title"]: item for item in response.json()["data"]}


async def _fixture(admin_client, api_prefix, email):
    """One task left open and one finished today, both logged five days ago."""
    client, user_id = await _employee(admin_client, api_prefix, email)
    still_open = await _log_task(
        client, api_prefix, title="Migrate the reporting job", date=FIVE_DAYS_AGO
    )
    finished = await _log_task(
        client, api_prefix, title="Patch the login banner", date=FIVE_DAYS_AGO
    )
    done = await client.patch(
        f"{api_prefix}/tasks/{finished['id']}/progress", json={"progress": "DONE"}
    )
    assert done.status_code == 200, done.text
    return client, user_id, still_open, finished


async def test_an_open_task_still_shows_up_five_days_later(admin_client, api_prefix):
    client, _, _, _ = await _fixture(admin_client, api_prefix, "carry1@example.com")

    today = await _day(client, api_prefix, date_key(TODAY))
    await client.aclose()

    assert set(today) == {"Migrate the reporting job", "Patch the login banner"}
    assert today["Migrate the reporting job"]["is_carried_forward"] is True
    assert today["Migrate the reporting job"]["days_open"] == 5
    assert today["Migrate the reporting job"]["date"] == FIVE_DAYS_AGO


async def test_a_finished_task_stops_carrying_the_day_after_it_closed(
    admin_client, api_prefix
):
    client, _, _, _ = await _fixture(admin_client, api_prefix, "carry2@example.com")

    tomorrow = await _day(client, api_prefix, TOMORROW)
    await client.aclose()

    # It was finished today, so today's list still shows it; tomorrow's does not.
    assert set(tomorrow) == {"Migrate the reporting job"}


async def test_a_task_does_not_appear_before_the_day_it_was_logged(
    admin_client, api_prefix
):
    client, _, _, _ = await _fixture(admin_client, api_prefix, "carry3@example.com")

    before = await _day(client, api_prefix, date_key(TODAY - timedelta(days=6)))
    on_the_day = await _day(client, api_prefix, FIVE_DAYS_AGO)
    await client.aclose()

    assert before == {}
    assert len(on_the_day) == 2
    assert on_the_day["Migrate the reporting job"]["is_carried_forward"] is False
    assert on_the_day["Migrate the reporting job"]["days_open"] == 0


async def test_todays_endpoint_includes_what_rolled_over(admin_client, api_prefix):
    client, _, _, _ = await _fixture(admin_client, api_prefix, "carry4@example.com")

    response = await client.get(f"{api_prefix}/tasks/today")
    await client.aclose()

    titles = {item["title"] for item in response.json()["data"]}
    assert titles == {"Migrate the reporting job", "Patch the login banner"}


async def test_a_long_running_task_is_flagged_as_stale(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "stale@example.com")
    long_ago = date_key(TODAY - timedelta(days=30))

    async with client:
        await _log_task(client, api_prefix, title="Rewrite the importer", date=long_ago)
        recent = await _log_task(
            client, api_prefix, title="Answer the support ticket", date=date_key(TODAY)
        )
        today = await _day(client, api_prefix, date_key(TODAY))

    assert today["Rewrite the importer"]["days_open"] == 30
    assert today["Rewrite the importer"]["is_stale"] is True
    assert today["Answer the support ticket"]["is_stale"] is False
    assert recent["is_stale"] is False


async def test_reopening_a_finished_task_puts_it_back_on_later_days(
    admin_client, api_prefix
):
    client, _, _, finished = await _fixture(admin_client, api_prefix, "reopen@example.com")

    await client.patch(
        f"{api_prefix}/tasks/{finished['id']}/progress",
        json={"progress": "BLOCKED", "note": "It came back."},
    )
    tomorrow = await _day(client, api_prefix, TOMORROW)
    await client.aclose()

    assert "Patch the login banner" in tomorrow
    assert tomorrow["Patch the login banner"]["days_open"] == 6


# --------------------------------------------------------------------------- #
# who may read whose day
# --------------------------------------------------------------------------- #
async def test_an_administrator_can_open_an_employees_day(admin_client, api_prefix):
    client, user_id, _, _ = await _fixture(admin_client, api_prefix, "adminday@example.com")
    await client.aclose()

    day = await _day(admin_client, api_prefix, date_key(TODAY), user_id=user_id)

    assert len(day) == 2


async def test_an_employee_cannot_open_somebody_elses_day(admin_client, api_prefix):
    client, user_id, _, _ = await _fixture(admin_client, api_prefix, "victim@example.com")
    await client.aclose()

    other, _ = await _employee(admin_client, api_prefix, "nosy@example.com")
    async with other:
        response = await other.get(
            f"{api_prefix}/tasks/day",
            params={"date": date_key(TODAY), "user_id": user_id},
        )

    assert response.status_code == 403
