"""Correction requests: raising, approving, rejecting, withdrawing."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import OFFICE_LOCATION, authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _seed_shift(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


async def _employee(admin_client, api_prefix, email="fixer@example.com"):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session


def _yesterday() -> str:
    return date_key(org_today() - timedelta(days=1))


# --------------------------------------------------------------------------- #
# raising a request
# --------------------------------------------------------------------------- #
async def test_an_employee_can_request_a_missing_punch_out(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    day = _yesterday()

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Forgot to punch out before leaving for the client visit.",
            },
        )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "PENDING"
    assert data["date"] == day


async def test_a_reason_is_required(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    day = _yesterday()

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "x",
            },
        )

    assert response.status_code == 422


async def test_a_missed_punch_out_request_needs_a_punch_out_time(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": _yesterday(),
                "type": "MISSED_PUNCH_OUT",
                "reason": "I forgot to punch out yesterday evening.",
            },
        )

    assert response.status_code == 422


async def test_a_punch_out_before_the_punch_in_is_rejected(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    day = _yesterday()

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T13:00:00Z",
                "requested_punch_out": f"{day}T04:00:00Z",
                "reason": "Times the wrong way round on purpose.",
            },
        )

    assert response.status_code == 422


async def test_a_correction_cannot_be_requested_for_the_future(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    tomorrow = date_key(org_today() + timedelta(days=1))

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": tomorrow,
                "type": "FULL_DAY",
                "requested_punch_in": f"{tomorrow}T04:00:00Z",
                "requested_punch_out": f"{tomorrow}T13:00:00Z",
                "reason": "Planning ahead, which should not be allowed.",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FUTURE_DATE"


async def test_a_day_older_than_the_correction_window_is_refused(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json={"correction_window_days": 3})
    client, _ = await _employee(admin_client, api_prefix)
    old_day = date_key(org_today() - timedelta(days=30))

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": old_day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{old_day}T04:00:00Z",
                "requested_punch_out": f"{old_day}T13:00:00Z",
                "reason": "Trying to rewrite last month.",
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "CORRECTION_WINDOW_EXPIRED"
    assert body["error"]["details"]["window_days"] == 3


async def test_only_one_pending_request_per_day(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    day = _yesterday()
    body = {
        "date": day,
        "type": "FULL_DAY",
        "requested_punch_in": f"{day}T04:00:00Z",
        "requested_punch_out": f"{day}T13:00:00Z",
        "reason": "Forgot to punch in and out that day.",
    }

    async with client:
        assert (await client.post(f"{api_prefix}/corrections", json=body)).status_code == 201
        duplicate = await client.post(f"{api_prefix}/corrections", json=body)

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CORRECTION_ALREADY_PENDING"


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
async def test_approving_a_correction_rewrites_the_day(admin_client, api_prefix):
    """The whole point: a red day becomes green once the request is approved."""
    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix)
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Was at the client site all day; forgot to punch.",
            },
        )
    correction_id = created.json()["data"]["id"]

    # Before: the day is absent.
    before = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": int(day[:4]), "month": int(day[5:7])},
    )
    before_day = next(d for d in before.json()["data"]["days"] if d["date"] == day)
    assert before_day["status"] == "ABSENT"

    approved = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve",
        json={"note": "Confirmed with the team lead."},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "APPROVED"

    after = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": int(day[:4]), "month": int(day[5:7])},
    )
    after_day = next(d for d in after.json()["data"]["days"] if d["date"] == day)
    assert after_day["status"] == "PRESENT"
    assert after_day["worked_minutes"] == 540
    assert after_day["color"] == "green"


async def test_approving_a_missed_punch_out_closes_the_open_session(
    admin_client, api_prefix, db
):
    from app.core.datetime import utc_now

    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="openday@example.com")
    day = _yesterday()

    await db["attendance"].insert_one(
        {
            "user_id": session["user"]["id"],
            "date": day,
            "sessions": [
                {"punch_in": {"at": utc_now() - timedelta(days=1, hours=9)}, "punch_out": None}
            ],
            "status": "MISSING_PUNCH_OUT",
            "is_deleted": False,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "MISSED_PUNCH_OUT",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Left in a hurry and forgot to punch out.",
            },
        )
    correction_id = created.json()["data"]["id"]

    approved = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve", json={}
    )

    assert approved.status_code == 200
    record = await db["attendance"].find_one({"date": day})
    assert len(record["sessions"]) == 1
    assert record["sessions"][0]["punch_out"] is not None
    assert record["sessions"][0]["punch_out"]["source"] == "CORRECTION"


async def test_rejecting_a_correction_leaves_the_day_alone(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="denied@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Asking for a day I did not actually work.",
            },
        )
    correction_id = created.json()["data"]["id"]

    rejected = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/reject",
        json={"note": "No record of work that day."},
    )

    assert rejected.json()["data"]["status"] == "REJECTED"
    calendar = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": int(day[:4]), "month": int(day[5:7])},
    )
    day_row = next(d for d in calendar.json()["data"]["days"] if d["date"] == day)
    assert day_row["status"] == "ABSENT"


async def test_a_decided_request_cannot_be_decided_again(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="twice3@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Forgot to punch that day entirely.",
            },
        )
    correction_id = created.json()["data"]["id"]

    await admin_client.post(f"{api_prefix}/corrections/{correction_id}/approve", json={})
    again = await admin_client.post(f"{api_prefix}/corrections/{correction_id}/reject", json={})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CORRECTION_ALREADY_DECIDED"


async def test_the_approval_records_who_decided_and_in_what_role(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="whodecided@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Client site visit, no access to a computer.",
            },
        )
    correction_id = created.json()["data"]["id"]

    approved = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve", json={"note": "Verified."}
    )

    data = approved.json()["data"]
    assert data["approver_email"] == "admin@example.com"
    assert data["approver_role"] == "ADMIN"
    assert data["decided_at"] is not None


async def test_an_employee_cannot_approve_their_own_request(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="selfapprove@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Trying to approve my own correction.",
            },
        )
        correction_id = created.json()["data"]["id"]
        response = await client.post(
            f"{api_prefix}/corrections/{correction_id}/approve", json={}
        )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# listing and withdrawal
# --------------------------------------------------------------------------- #
async def test_an_employee_sees_only_their_own_requests(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    day = _yesterday()
    body = {
        "date": day,
        "type": "FULL_DAY",
        "requested_punch_in": f"{day}T04:00:00Z",
        "requested_punch_out": f"{day}T13:00:00Z",
        "reason": "Forgot to punch on that day.",
    }

    first, _ = await _employee(admin_client, api_prefix, email="mine1@example.com")
    async with first:
        await first.post(f"{api_prefix}/corrections", json=body)

    second, _ = await _employee(admin_client, api_prefix, email="mine2@example.com")
    async with second:
        await second.post(f"{api_prefix}/corrections", json=body)
        mine = await second.get(f"{api_prefix}/corrections/mine")

    assert mine.json()["data"]["pagination"]["total"] == 1

    queue = await admin_client.get(f"{api_prefix}/corrections")
    assert queue.json()["data"]["pagination"]["total"] == 2


async def test_an_employee_cannot_read_the_whole_queue(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="peek@example.com")

    async with client:
        response = await client.get(f"{api_prefix}/corrections")

    assert response.status_code == 403


async def test_an_employee_can_withdraw_a_pending_request(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="withdraw@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Raised this one by mistake.",
            },
        )
        correction_id = created.json()["data"]["id"]
        cancelled = await client.post(f"{api_prefix}/corrections/{correction_id}/cancel")

    assert cancelled.json()["data"]["status"] == "CANCELLED"


async def test_one_employee_cannot_withdraw_anothers_request(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    day = _yesterday()
    owner, _ = await _employee(admin_client, api_prefix, email="owner@example.com")
    async with owner:
        created = await owner.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "A request belonging to somebody else.",
            },
        )
    correction_id = created.json()["data"]["id"]

    intruder, _ = await _employee(admin_client, api_prefix, email="intruder@example.com")
    async with intruder:
        response = await intruder.post(f"{api_prefix}/corrections/{correction_id}/cancel")

    assert response.status_code == 404


async def test_the_pending_count_drives_the_admin_badge(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="badge@example.com")
    day = _yesterday()

    async with client:
        await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Needs a decision from the administrator.",
            },
        )

    response = await admin_client.get(f"{api_prefix}/corrections/pending-count")

    assert response.json()["data"]["pending"] == 1


async def test_an_approval_is_written_to_the_audit_trail(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="trail@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Client visit; no computer available.",
            },
        )
    correction_id = created.json()["data"]["id"]
    await admin_client.post(f"{api_prefix}/corrections/{correction_id}/approve", json={})

    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity": "CORRECTION"}
    )
    actions = {entry["action"] for entry in logs.json()["data"]["items"]}
    assert {"CREATE", "APPROVE"} <= actions


async def test_punching_normally_still_works_after_a_correction(admin_client, api_prefix):
    """An approved correction must not corrupt today's punching."""
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="both@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Yesterday was missed entirely.",
            },
        )
        correction_id = created.json()["data"]["id"]

    await admin_client.post(f"{api_prefix}/corrections/{correction_id}/approve", json={})

    async with authed_client(
        await _employee_token(admin_client, api_prefix, "both@example.com")
    ) as same_employee:
        response = await same_employee.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "IN_PROGRESS"


async def _employee_token(admin_client, api_prefix, email: str) -> str:
    response = await admin_client.post(
        f"{api_prefix}/auth/login", json={"email": email, "password": "Employee123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["tokens"]["access_token"]
