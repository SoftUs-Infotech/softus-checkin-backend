"""The admin task explorer: the month grid, the week summary and bulk decisions."""

from datetime import timedelta

import pytest
from bson import ObjectId

from app.core.datetime import date_key, month_range, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

ANCHOR = org_today()
FIRST, LAST = month_range(ANCHOR.year, ANCHOR.month)
EARLY = date_key(FIRST)
LATE = date_key(ANCHOR)
SAME_DAY = EARLY == LATE


async def _employee(admin_client, api_prefix, email):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session["user"]["id"]


async def _log_task(client, api_prefix, *, title, date):
    response = await client.post(
        f"{api_prefix}/tasks", json={"date": date, "title": title}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _month_fixture(admin_client, api_prefix, email):
    """Three tasks: two logged on the 1st (one finished today), one logged today."""
    client, user_id = await _employee(admin_client, api_prefix, email)
    async with client:
        await _log_task(client, api_prefix, title="Open since the first", date=EARLY)
        finished = await _log_task(
            client, api_prefix, title="Finished today", date=EARLY
        )
        await _log_task(client, api_prefix, title="Started today", date=LATE)
        done = await client.patch(
            f"{api_prefix}/tasks/{finished['id']}/progress", json={"progress": "DONE"}
        )
        assert done.status_code == 200, done.text
    return user_id


async def _calendar(client, api_prefix, **params):
    response = await client.get(f"{api_prefix}/tasks/calendar", params=params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return data, {day["date"]: day for day in data["days"]}


# --------------------------------------------------------------------------- #
# the month grid
# --------------------------------------------------------------------------- #
async def test_the_grid_covers_every_day_of_the_month(admin_client, api_prefix):
    user_id = await _month_fixture(admin_client, api_prefix, "grid1@example.com")

    data, days = await _calendar(admin_client, api_prefix, user_id=user_id)

    assert data["from_date"] == date_key(FIRST)
    assert data["to_date"] == date_key(LAST)
    assert len(data["days"]) == (LAST - FIRST).days + 1
    assert data["user_id"] == user_id


async def test_a_day_counts_its_own_tasks_and_everything_carried_into_it(
    admin_client, api_prefix
):
    user_id = await _month_fixture(admin_client, api_prefix, "grid2@example.com")

    _, days = await _calendar(admin_client, api_prefix, user_id=user_id)

    assert days[LATE]["total"] == 3
    assert days[LATE]["done"] == 1
    assert days[LATE]["carried_in"] == (0 if SAME_DAY else 2)
    assert days[EARLY]["carried_in"] == 0
    assert days[EARLY]["total"] == (3 if SAME_DAY else 2)


async def test_only_the_still_open_tasks_reach_the_days_after_today(
    admin_client, api_prefix
):
    if ANCHOR == LAST:
        pytest.skip("Today is the last day of the month; there is no day after it.")
    user_id = await _month_fixture(admin_client, api_prefix, "grid3@example.com")

    _, days = await _calendar(admin_client, api_prefix, user_id=user_id)
    after = days[date_key(ANCHOR + timedelta(days=1))]

    assert after["total"] == 2
    assert after["carried_in"] == 2
    assert after["done"] == 0


async def test_the_month_summary_counts_what_was_logged_in_it(admin_client, api_prefix):
    user_id = await _month_fixture(admin_client, api_prefix, "grid4@example.com")

    data, _ = await _calendar(admin_client, api_prefix, user_id=user_id)

    summary = data["summary"]
    assert (summary["logged"], summary["completed"], summary["still_open"]) == (3, 1, 2)
    # Only the task left open since the first can be stale, and only once the
    # month has run long enough for it to cross the threshold.
    aged = (ANCHOR - FIRST).days >= data["stale_after_days"]
    assert summary["stale"] == (1 if aged else 0)


async def test_an_empty_month_is_all_zeroes(admin_client, api_prefix):
    user_id = await _month_fixture(admin_client, api_prefix, "grid5@example.com")
    earlier = FIRST - timedelta(days=1)

    data, _ = await _calendar(
        admin_client, api_prefix, user_id=user_id, year=earlier.year, month=earlier.month
    )

    assert data["summary"]["logged"] == 0
    assert all(day["total"] == 0 for day in data["days"])
    assert all(week["completed"] == 0 for week in data["weeks"])


# --------------------------------------------------------------------------- #
# the week summary
# --------------------------------------------------------------------------- #
async def test_the_weeks_tile_the_month_without_gaps(admin_client, api_prefix):
    user_id = await _month_fixture(admin_client, api_prefix, "week1@example.com")

    data, _ = await _calendar(admin_client, api_prefix, user_id=user_id)
    weeks = data["weeks"]

    assert [week["week"] for week in weeks] == list(range(1, len(weeks) + 1))
    assert weeks[0]["from_date"] == date_key(FIRST)
    assert weeks[-1]["to_date"] == date_key(LAST)
    for previous, following in zip(weeks, weeks[1:], strict=False):
        assert previous["to_date"] < following["from_date"]


async def test_a_week_reports_completed_in_progress_and_carried_forward(
    admin_client, api_prefix
):
    user_id = await _month_fixture(admin_client, api_prefix, "week2@example.com")

    data, _ = await _calendar(admin_client, api_prefix, user_id=user_id)
    weeks = data["weeks"]
    this_week = next(
        week for week in weeks if week["from_date"] <= LATE <= week["to_date"]
    )

    # Exactly one task was finished this month, and it was finished today.
    assert sum(week["completed"] for week in weeks) == 1
    assert this_week["completed"] == 1
    # Nothing predates the first of the month, so nothing carries into week one.
    assert weeks[0]["carried_forward"] == 0
    assert this_week["carried_forward"] == (0 if this_week == weeks[0] else 2)
    # Two tasks are still open at the end of the month.
    assert weeks[-1]["in_progress"] == 2


# --------------------------------------------------------------------------- #
# who may look
# --------------------------------------------------------------------------- #
async def test_an_employee_may_open_their_own_explorer(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "self@example.com")

    async with client:
        without_id, _ = await _calendar(client, api_prefix)
        with_id, _ = await _calendar(client, api_prefix, user_id=user_id)

    assert without_id["user_id"] == user_id
    assert with_id["user_id"] == user_id


async def test_an_employee_cannot_open_somebody_elses_explorer(admin_client, api_prefix):
    user_id = await _month_fixture(admin_client, api_prefix, "watched@example.com")

    other, _ = await _employee(admin_client, api_prefix, "watcher@example.com")
    async with other:
        response = await other.get(
            f"{api_prefix}/tasks/calendar", params={"user_id": user_id}
        )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# bulk decisions
# --------------------------------------------------------------------------- #
async def test_a_bulk_decision_reports_each_id_separately(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "bulk1@example.com")
    async with client:
        first = await _log_task(client, api_prefix, title="Bulk one", date=LATE)
        second = await _log_task(client, api_prefix, title="Bulk two", date=LATE)

    own = await _log_task(admin_client, api_prefix, title="The admin's own", date=LATE)
    missing = str(ObjectId())

    response = await admin_client.post(
        f"{api_prefix}/tasks/bulk-decision",
        json={
            "task_ids": [first["id"], "not-an-id", second["id"], missing, own["id"]],
            "decision": "APPROVED",
            "note": "Cleared in the Monday review.",
        },
    )

    data = response.json()["data"]
    assert response.status_code == 200, response.text
    assert (data["requested"], data["succeeded"], data["failed"]) == (5, 2, 3)

    outcomes = {result["task_id"]: result for result in data["results"]}
    assert outcomes[first["id"]]["success"] is True
    assert outcomes[second["id"]]["success"] is True
    assert outcomes["not-an-id"]["error_code"] == "TASK_NOT_FOUND"
    assert outcomes[missing]["error_code"] == "TASK_NOT_FOUND"
    assert outcomes[own["id"]]["error_code"] == "CANNOT_REVIEW_OWN_TASK"


async def test_the_shared_note_lands_on_every_task_it_touched(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "bulk2@example.com")
    async with client:
        first = await _log_task(client, api_prefix, title="Note one", date=LATE)
        second = await _log_task(client, api_prefix, title="Note two", date=LATE)

    await admin_client.post(
        f"{api_prefix}/tasks/bulk-decision",
        json={
            "task_ids": [first["id"], second["id"]],
            "decision": "CHANGES_REQUESTED",
            "note": "Add the ticket numbers, both of you.",
        },
    )

    queue = await admin_client.get(f"{api_prefix}/tasks")
    items = {item["title"]: item for item in queue.json()["data"]["items"]}

    for title in ("Note one", "Note two"):
        assert items[title]["status"] == "CHANGES_REQUESTED"
        assert items[title]["timeline"][-1]["note"] == "Add the ticket numbers, both of you."
        assert items[title]["timeline"][-1]["from_state"] == "SUBMITTED"


async def test_a_repeated_id_is_decided_once(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "bulk3@example.com")
    async with client:
        task = await _log_task(client, api_prefix, title="Only once", date=LATE)

    response = await admin_client.post(
        f"{api_prefix}/tasks/bulk-decision",
        json={"task_ids": [task["id"], task["id"]], "decision": "REJECTED"},
    )

    data = response.json()["data"]
    assert (data["requested"], data["succeeded"], data["failed"]) == (1, 1, 0)


async def test_submitted_is_not_a_decision(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/tasks/bulk-decision",
        json={"task_ids": [str(ObjectId())], "decision": "SUBMITTED"},
    )

    assert response.status_code == 422


async def test_an_empty_batch_is_rejected(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/tasks/bulk-decision",
        json={"task_ids": [], "decision": "APPROVED"},
    )

    assert response.status_code == 422


async def test_an_employee_cannot_decide_in_bulk(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "bulk4@example.com")
    async with client:
        task = await _log_task(client, api_prefix, title="Not yours to approve", date=LATE)
        response = await client.post(
            f"{api_prefix}/tasks/bulk-decision",
            json={"task_ids": [task["id"]], "decision": "APPROVED"},
        )

    assert response.status_code == 403
