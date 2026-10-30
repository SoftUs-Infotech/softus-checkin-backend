"""Announcements, events, notifications and the dashboard summary."""


import pytest

from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _employee(admin_client, api_prefix, email="reader2@example.com", **invite):
    session = await invite_and_accept(admin_client, api_prefix, email=email, **invite)
    return authed_client(session["tokens"]["access_token"]), session


# --------------------------------------------------------------------------- #
# announcements
# --------------------------------------------------------------------------- #


async def test_an_announcement_can_be_aimed_at_one_department(admin_client, api_prefix):
    engineering, _ = await _employee(admin_client, api_prefix, email="eng@example.com")
    await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": "sales@example.com", "department": "Sales"},
    )

    # Put the first employee in Engineering.
    async with engineering:
        await engineering.patch(f"{api_prefix}/auth/me", json={"department": "Engineering"})
        await admin_client.post(
            f"{api_prefix}/announcements",
            json={
                "title": "Engineering all-hands",
                "body": "Sprint review at 4pm.",
                "audience_departments": ["Engineering"],
            },
        )
        feed = await engineering.get(f"{api_prefix}/announcements")

    assert len(feed.json()["data"]) == 1
