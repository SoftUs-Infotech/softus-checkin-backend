"""Work from home: mode of work, the screen-capture punch gate, review and deletion."""

from datetime import timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.datetime import org_today, utc_now
from app.db.mongo import Collections
from app.utils.storage import reset_storage_cache
from tests.conftest import OFFICE_LOCATION, authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

#: Enough of a JPEG for the type check, which reads the first bytes only.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256


@pytest.fixture(autouse=True)
def _storage_in_tmp(tmp_path, monkeypatch):
    """Every test writes its files to its own folder, never to ./uploads."""
    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_STORAGE_DIR", str(tmp_path / "uploads"))
    reset_storage_cache()
    yield tmp_path / "uploads"
    reset_storage_cache()


async def _employee(admin_client, api_prefix, email: str, *, wfh: bool):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    user_id = session["user"]["id"]
    if wfh:
        response = await admin_client.patch(
            f"{api_prefix}/users/{user_id}", json={"work_mode": "WFH"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["work_mode"] == "WFH"
    return authed_client(session["tokens"]["access_token"]), user_id


async def _upload(client, api_prefix, data: bytes = JPEG):
    return await client.post(
        f"{api_prefix}/screenshots",
        files={"file": ("screen.jpg", data, "image/jpeg")},
        data={"width": "1600", "height": "900"},
    )


async def test_mode_of_work_is_set_by_an_admin_and_filters_the_roster(admin_client, api_prefix):
    home, _ = await _employee(admin_client, api_prefix, "home@example.com", wfh=True)
    office, _ = await _employee(admin_client, api_prefix, "office@example.com", wfh=False)
    await home.aclose()
    await office.aclose()

    wfh = await admin_client.get(f"{api_prefix}/users", params={"work_mode": "WFH"})
    assert [u["email"] for u in wfh.json()["data"]["items"]] == ["home@example.com"]

    # "Office" includes accounts that predate the field — the admin among them.
    office_list = await admin_client.get(f"{api_prefix}/users", params={"work_mode": "OFFICE"})
    emails = {u["email"] for u in office_list.json()["data"]["items"]}
    assert "office@example.com" in emails and "home@example.com" not in emails


async def test_working_from_home_needs_a_fresh_screenshot_to_punch_in(
    admin_client, api_prefix, db
):
    client, user_id = await _employee(admin_client, api_prefix, "wfh@example.com", wfh=True)
    async with client:
        today = await client.get(f"{api_prefix}/attendance/today")
        assert today.json()["data"]["screen_capture_required"] is True

        refused = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "SCREEN_CAPTURE_REQUIRED"

        # A screenshot from earlier in the day is not capture running now.
        await db[Collections.SCREENSHOTS].insert_one(
            {
                "user_id": user_id,
                "date": org_today().isoformat(),
                "captured_at": utc_now() - timedelta(minutes=30),
                "storage_key": "screenshots/x/old.jpg",
                "storage_provider": "local",
                "content_type": "image/jpeg",
                "size_bytes": 1,
            }
        )
        stale = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert stale.json()["error"]["code"] == "SCREEN_CAPTURE_REQUIRED"

        uploaded = await _upload(client, api_prefix)
        assert uploaded.status_code == 201, uploaded.text

        punched = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert punched.status_code == 200, punched.text


async def test_office_staff_punch_as_before_and_cannot_upload(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "desk@example.com", wfh=False)
    async with client:
        today = await client.get(f"{api_prefix}/attendance/today")
        assert today.json()["data"]["screen_capture_required"] is False

        refused = await _upload(client, api_prefix)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "NOT_WORKING_FROM_HOME"

        punched = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert punched.status_code == 200, punched.text


async def test_an_upload_has_to_be_an_image(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "fake@example.com", wfh=True)
    async with client:
        response = await _upload(client, api_prefix, b"<html>not a picture</html>")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NOT_AN_IMAGE"


async def test_the_admin_sees_screenshots_through_a_signed_link(
    admin_client, api_prefix, _storage_in_tmp: Path
):
    client, user_id = await _employee(admin_client, api_prefix, "seen@example.com", wfh=True)
    async with client:
        assert (await _upload(client, api_prefix)).status_code == 201

    summary = (await admin_client.get(f"{api_prefix}/screenshots/summary")).json()["data"]
    person = next(p for p in summary["people"] if p["id"] == user_id)
    assert person["on_day"] == 1 and person["total_bytes"] == len(JPEG)
    assert summary["totals"]["people_wfh"] == 1

    listed = await admin_client.get(f"{api_prefix}/screenshots", params={"user_id": user_id})
    [shot] = listed.json()["data"]
    assert shot["url"].startswith("/files?")

    # The link works without a bearer token — an <img> cannot send one…
    async with authed_client("unused") as anonymous:
        anonymous.headers.pop("Authorization")
        image = await anonymous.get(f"{api_prefix}{shot['url']}")
        assert image.status_code == 200
        assert image.content == JPEG

        # …and only as signed.
        forged = await anonymous.get(f"{api_prefix}{shot['url'].replace('sig=', 'sig=0')}")
        assert forged.status_code == 403

    # The days list knows about today.
    days = await admin_client.get(f"{api_prefix}/screenshots/days", params={"user_id": user_id})
    assert days.json()["data"][0]["count"] == 1


async def test_deleting_removes_the_files_not_just_the_records(
    admin_client, api_prefix, db, _storage_in_tmp: Path
):
    client, user_id = await _employee(admin_client, api_prefix, "gone@example.com", wfh=True)
    async with client:
        for _ in range(3):
            assert (await _upload(client, api_prefix)).status_code == 201
    assert len(list(_storage_in_tmp.rglob("*.jpg"))) == 3

    listed = await admin_client.get(f"{api_prefix}/screenshots", params={"user_id": user_id})
    first_id = listed.json()["data"][0]["id"]

    one = await admin_client.post(f"{api_prefix}/screenshots/delete", json={"ids": [first_id]})
    assert one.json()["data"] == {"deleted": 1, "freed_bytes": len(JPEG), "failed": 0}
    assert len(list(_storage_in_tmp.rglob("*.jpg"))) == 2

    day = await admin_client.post(
        f"{api_prefix}/screenshots/delete",
        json={"user_id": user_id, "date": org_today().isoformat()},
    )
    assert day.json()["data"]["deleted"] == 2
    assert list(_storage_in_tmp.rglob("*.jpg")) == []
    assert await db[Collections.SCREENSHOTS].count_documents({}) == 0

    logged = await db[Collections.AUDIT_LOGS].count_documents({"entity": "SCREENSHOT"})
    assert logged == 2


async def test_delete_everything_and_the_scope_rule(admin_client, api_prefix, _storage_in_tmp):
    client, _ = await _employee(admin_client, api_prefix, "all@example.com", wfh=True)
    async with client:
        await _upload(client, api_prefix)

    vague = await admin_client.post(f"{api_prefix}/screenshots/delete", json={})
    assert vague.status_code == 422

    everything = await admin_client.post(
        f"{api_prefix}/screenshots/delete", json={"everything": True}
    )
    assert everything.json()["data"]["deleted"] == 1
    assert list(_storage_in_tmp.rglob("*.jpg")) == []


async def test_review_and_deletion_are_for_administrators(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "nosy@example.com", wfh=True)
    async with client:
        for request in (
            client.get(f"{api_prefix}/screenshots/summary"),
            client.get(f"{api_prefix}/screenshots", params={"user_id": user_id}),
            client.post(f"{api_prefix}/screenshots/delete", json={"everything": True}),
        ):
            assert (await request).status_code == 403


# --------------------------------------------------------------------------- #
# stopping sharing ends the session
# --------------------------------------------------------------------------- #
async def _punched_in_from_home(admin_client, api_prefix, email: str):
    client, user_id = await _employee(admin_client, api_prefix, email, wfh=True)
    assert (await _upload(client, api_prefix)).status_code == 201
    punched = await client.post(
        f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
    )
    assert punched.status_code == 200, punched.text
    return client, user_id


async def test_stopping_sharing_punches_out_and_resuming_is_one_step(
    admin_client, api_prefix, db
):
    client, _ = await _punched_in_from_home(admin_client, api_prefix, "stop@example.com")
    try:
        stopped = await client.post(f"{api_prefix}/screenshots/stopped")
        assert stopped.status_code == 200, stopped.text
        [session] = stopped.json()["data"]["sessions"]
        assert session["closed_reason"] == "SCREEN_SHARE_STOPPED"
        assert session["punch_out"]["source"] == "SYSTEM"

        today = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]
        assert today["can_punch_out"] is False

        # Saying it twice changes nothing.
        again = await client.post(f"{api_prefix}/screenshots/stopped")
        assert again.status_code == 200 and again.json()["data"] is None

        # Coming back needs the screen shared again…
        await db[Collections.SCREENSHOTS].update_many(
            {}, {"$set": {"captured_at": utc_now() - timedelta(minutes=10)}}
        )
        refused = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert refused.json()["error"]["code"] == "SCREEN_CAPTURE_REQUIRED"

        # …and then works at once: the 60-second gap between punches is for
        # double taps, not for somebody the system punched out.
        assert (await _upload(client, api_prefix)).status_code == 201
        resumed = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert resumed.status_code == 200, resumed.text
        assert len(resumed.json()["data"]["sessions"]) == 2
    finally:
        await client.aclose()


async def test_a_screen_that_goes_quiet_is_punched_out_at_its_last_sign_of_life(
    admin_client, api_prefix, db
):
    from app.services.attendance import close_unshared_sessions

    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "quiet@example.com")
    await client.aclose()

    # Heard from a minute ago: still sharing, nothing happens.
    assert await close_unshared_sessions() == 0

    went_quiet = utc_now() - timedelta(minutes=10)
    await db[Collections.CAPTURE_HEARTBEATS].update_one(
        {"_id": user_id}, {"$set": {"last_seen_at": went_quiet}}
    )
    # The punch-in itself was "just now", so pretend it was earlier too.
    await db[Collections.ATTENDANCE].update_one(
        {"user_id": user_id},
        {"$set": {"sessions.0.punch_in.at": went_quiet - timedelta(hours=2)}},
    )

    assert await close_unshared_sessions() == 1
    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    session = record["sessions"][0]
    assert session["closed_reason"] == "SCREEN_SHARE_STOPPED"
    # Ended when the screen went quiet, not when the job noticed.
    assert abs((session["punch_out"]["at"] - went_quiet).total_seconds()) < 1
    assert record["worked_minutes"] == 120

    notice = await db[Collections.NOTIFICATIONS].find_one(
        {"user_id": user_id, "title": "You were punched out"}
    )
    assert notice is not None

    # Already ended: a second run leaves it alone.
    assert await close_unshared_sessions() == 0


async def test_heartbeats_keep_a_session_alive_and_are_for_home_workers_only(
    admin_client, api_prefix
):
    from app.services.attendance import close_unshared_sessions

    client, _ = await _punched_in_from_home(admin_client, api_prefix, "alive@example.com")
    try:
        beat = await client.post(f"{api_prefix}/screenshots/heartbeat")
        assert beat.status_code == 200
    finally:
        await client.aclose()
    assert await close_unshared_sessions() == 0

    office, _ = await _employee(admin_client, api_prefix, "desk2@example.com", wfh=False)
    async with office:
        assert (await office.post(f"{api_prefix}/screenshots/heartbeat")).status_code == 403
        assert (await office.post(f"{api_prefix}/screenshots/stopped")).status_code == 403
