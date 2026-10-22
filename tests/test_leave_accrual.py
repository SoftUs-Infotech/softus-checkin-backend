"""Leave accrual: what a policy credits, when, and only once."""

from datetime import date, timedelta

import pytest

from app.core.datetime import org_today
from app.db.mongo import Collections
from app.services import leave as leave_service
from app.services import leave_type as leave_type_service

pytestmark = pytest.mark.usefixtures("db")

YEAR = org_today().year


async def _leave_type(admin_client, api_prefix, **overrides) -> dict:
    body = {"name": "Sick Leave", "code": "SL", **overrides}
    response = await admin_client.post(f"{api_prefix}/leave-types", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _employee(admin_client, api_prefix, email, *, joining_date=None) -> str:
    """Invite and accept, optionally recording a joining date. Returns the id."""
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={
            "email": email,
            "first_name": "Ada",
            "role": "EMPLOYEE",
            **({"joining_date": joining_date.isoformat()} if joining_date else {}),
        },
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["data"]["invite_url"].split("token=")[1]

    accepted = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={"token": token, "password": "Employee123", "first_name": "Ada",
              "last_name": "L"},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["data"]["user"]["id"]


async def _balance(db, user_id: str, leave_type_id: str, year: int = YEAR) -> dict:
    return await db[Collections.LEAVE_BALANCES].find_one(
        {"user_id": user_id, "leave_type_id": leave_type_id, "year": year}
    )


# --------------------------------------------------------------------------- #
# how much a cycle is worth
# --------------------------------------------------------------------------- #
async def test_monthly_accrual_credits_one_cycle_per_elapsed_month(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(admin_client, api_prefix, "monthly@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 4, 10))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 4


async def test_quarterly_accrual_credits_the_quarters_that_have_started(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "QUARTERLY", "amount_per_cycle": 3},
    )
    user_id = await _employee(admin_client, api_prefix, "quarterly@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 7, 5))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 9


async def test_yearly_accrual_credits_the_whole_year_at_once(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12},
    )
    user_id = await _employee(admin_client, api_prefix, "yearly@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 1, 2))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 12


async def test_half_a_day_a_month_is_a_configuration_not_a_special_case(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 0.5},
    )
    assert leave_type["annual_quota"] == 6  # implied by the policy
    user_id = await _employee(admin_client, api_prefix, "halfmonth@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 6, 30))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 3


# --------------------------------------------------------------------------- #
# when the credit lands
# --------------------------------------------------------------------------- #
async def test_crediting_at_the_start_of_a_cycle_pays_on_its_first_day(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1, "credit_at": "START"},
    )
    user_id = await _employee(admin_client, api_prefix, "atstart@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 3, 1))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 3


async def test_crediting_at_the_end_of_a_cycle_waits_for_it_to_finish(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1, "credit_at": "END"},
    )
    user_id = await _employee(admin_client, api_prefix, "atend@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 3, 30))
    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 2

    await leave_service.run_accruals(as_of=date(YEAR, 3, 31))
    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 3


# --------------------------------------------------------------------------- #
# idempotency and proration
# --------------------------------------------------------------------------- #
async def test_running_the_job_twice_credits_once(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(admin_client, api_prefix, "twice@example.com")

    first = await leave_service.run_accruals(as_of=date(YEAR, 5, 9))
    second = await leave_service.run_accruals(as_of=date(YEAR, 5, 9))

    balance = await _balance(db, user_id, leave_type["id"])
    assert first == 2 and second == 0  # the administrator accrues too
    assert balance["allocated"] == 5
    assert len(balance["accrual_credits"]) == 5


async def test_a_mid_cycle_joiner_is_prorated(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(
        admin_client, api_prefix, "joiner@example.com",
        joining_date=date(YEAR, 3, 16),
    )

    await leave_service.run_accruals(as_of=date(YEAR, 3, 20))

    balance = await _balance(db, user_id, leave_type["id"])
    # Sixteen of March's thirty-one days, and nothing for the months before.
    assert balance["allocated"] == round(16 / 31, 2)


async def test_the_waiting_period_delays_the_first_credit(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
        applicable_after_days=90,
    )
    user_id = await _employee(
        admin_client, api_prefix, "waiting@example.com", joining_date=date(YEAR, 1, 1)
    )

    await leave_service.run_accruals(as_of=date(YEAR, 3, 20))
    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 0

    await leave_service.run_accruals(as_of=date(YEAR, 4, 15))
    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 1


async def test_every_credit_is_announced(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(admin_client, api_prefix, "told@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 1, 1))

    notifications = await db[Collections.NOTIFICATIONS].find(
        {"user_id": user_id}
    ).to_list(length=10)
    assert len(notifications) == 1
    assert leave_type["name"] in notifications[0]["title"]


# --------------------------------------------------------------------------- #
# the year end
# --------------------------------------------------------------------------- #
async def _close_last_year(db, user_id: str, leave_type: dict, leftover: float) -> None:
    await db[Collections.LEAVE_BALANCES].insert_one(
        {
            "user_id": user_id,
            "leave_type_id": leave_type["id"],
            "year": YEAR - 1,
            "allocated": leftover,
            "carried_forward": 0,
            "used": 0,
            "pending": 0,
            "accrual_credits": [],
            "is_deleted": False,
        }
    )


async def test_an_unused_balance_lapses_at_the_year_end(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12,
                 "lapse_at_year_end": True},
    )
    user_id = await _employee(admin_client, api_prefix, "lapsed@example.com")
    await _close_last_year(db, user_id, leave_type, 8)

    await leave_service.get_or_create_balance(user_id, leave_type, YEAR)

    assert (await _balance(db, user_id, leave_type["id"]))["carried_forward"] == 0


async def test_carry_forward_is_capped(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12},
        carry_forward_max=5,
    )
    user_id = await _employee(admin_client, api_prefix, "capped@example.com")
    await _close_last_year(db, user_id, leave_type, 8)

    await leave_service.get_or_create_balance(user_id, leave_type, YEAR)

    assert (await _balance(db, user_id, leave_type["id"]))["carried_forward"] == 5


async def test_a_type_that_never_lapses_keeps_the_whole_leftover(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12,
                 "lapse_at_year_end": False},
    )
    user_id = await _employee(admin_client, api_prefix, "keeps@example.com")
    await _close_last_year(db, user_id, leave_type, 8)

    await leave_service.get_or_create_balance(user_id, leave_type, YEAR)

    assert (await _balance(db, user_id, leave_type["id"]))["carried_forward"] == 8


# --------------------------------------------------------------------------- #
# documents written before accrual was a policy
# --------------------------------------------------------------------------- #
async def test_a_legacy_leave_type_still_accrues(db, admin_client, api_prefix):
    """Only ``annual_quota`` and a frequency string — the shape v1 stored."""
    await db[Collections.LEAVE_TYPES].insert_one(
        {
            "name": "Casual Leave",
            "code": "CL",
            "annual_quota": 12,
            "accrual": "MONTHLY",
            "carry_forward_max": 0,
            "applicable_after_days": 0,
            "is_active": True,
            "is_deleted": False,
        }
    )
    leave_type = (await leave_type_service.list_leave_types())[0]
    user_id = await _employee(admin_client, api_prefix, "legacy@example.com")

    await leave_service.run_accruals(as_of=date(YEAR, 4, 10))

    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 4


async def test_a_legacy_leave_type_reads_back_as_a_policy(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Casual Leave", "code": "CL", "annual_quota": 12,
              "accrual": "MONTHLY"},
    )

    accrual = response.json()["data"]["accrual"]
    assert accrual == {
        "frequency": "MONTHLY",
        "amount_per_cycle": 1,
        "credit_at": "START",
        "lapse_at_year_end": True,
    }


# --------------------------------------------------------------------------- #
# reading a balance keeps it current between runs
# --------------------------------------------------------------------------- #
async def test_reading_a_balance_credits_what_is_already_earned(
    admin_client, api_prefix
):
    await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(admin_client, api_prefix, "reader@example.com")

    balances = await admin_client.get(f"{api_prefix}/leave/balances/{user_id}")

    months_elapsed = org_today().month
    assert balances.json()["data"][0]["allocated"] == months_elapsed


async def test_cycle_keys_and_due_dates_cover_the_year():
    from app.models.leave_type import AccrualPolicy

    policy = AccrualPolicy(frequency="QUARTERLY", amount_per_cycle=2, credit_at="END")
    cycles = leave_service.cycles_in_year(policy, 2026)

    assert [cycle.key for cycle in cycles] == [
        "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4",
    ]
    assert cycles[0].credit_on == date(2026, 3, 31)
    assert cycles[0].credit_on == cycles[1].start - timedelta(days=1)
