"""Blanking a field is an instruction, not an absence.

``update_user`` dropped every ``None`` in the payload, which collapsed two
different things: "I did not mention this field" and "I sent null, take it
out". An administrator could therefore fill in a job title and never remove it
again — the PATCH went through, reported success, and changed nothing.
"""

import pytest

from app.core.exceptions import ValidationError
from app.services import user as user_service

pytestmark = pytest.mark.asyncio


async def make_employee(db, **fields) -> str:
    from app.db.mongo import Collections

    result = await db[Collections.USERS].insert_one(
        {
            "email": "clearing@example.com",
            "role": "EMPLOYEE",
            "status": "ACTIVE",
            "is_deleted": False,
            "first_name": "Ada",
            "last_name": "Lovelace",
            **fields,
        }
    )
    return str(result.inserted_id)


async def test_a_job_title_can_be_taken_out_again(db):
    user_id = await make_employee(db, designation="Analyst")

    updated = await user_service.update_user(user_id, {"designation": None})

    assert updated.get("designation") is None


async def test_a_department_can_be_taken_out_again(db):
    user_id = await make_employee(db, department="Finance")

    updated = await user_service.update_user(user_id, {"department": None})

    assert updated.get("department") is None


async def test_clearing_the_shift_puts_somebody_back_on_the_default(db):
    user_id = await make_employee(db, shift_id="65f000000000000000000001")

    updated = await user_service.update_user(user_id, {"shift_id": None})

    # None means "no shift of your own", which resolves to the org default.
    assert updated.get("shift_id") is None


async def test_clearing_a_reporting_manager_does_not_look_the_manager_up(db):
    """A null must not be validated as an identifier, or clearing 404s."""
    user_id = await make_employee(db, reporting_manager_id="65f000000000000000000002")

    updated = await user_service.update_user(user_id, {"reporting_manager_id": None})

    assert updated.get("reporting_manager_id") is None


async def test_a_field_that_was_not_mentioned_is_still_left_alone(db):
    user_id = await make_employee(db, designation="Analyst", department="Finance")

    updated = await user_service.update_user(user_id, {"department": "Research"})

    assert updated["department"] == "Research"
    assert updated["designation"] == "Analyst"


async def test_a_decision_field_still_ignores_a_null(db):
    """"Clear it" is not a meaningful instruction for a role."""
    user_id = await make_employee(db)

    updated = await user_service.update_user(user_id, {"role": None})

    assert updated["role"] == "EMPLOYEE"


async def test_a_first_name_still_cannot_be_removed(db):
    user_id = await make_employee(db)

    updated = await user_service.update_user(user_id, {"first_name": None})

    assert updated["first_name"] == "Ada"


async def test_a_bad_identifier_is_still_refused(db):
    user_id = await make_employee(db)

    with pytest.raises(ValidationError):
        await user_service.update_user(user_id, {"shift_id": "not-an-id"})
