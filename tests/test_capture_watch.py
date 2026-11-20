"""Work from home: the app log, and sessions that end when capture is lost.

- The desktop app reports what happened on the computer; the server adds the
  punches and automatic punch-outs; only an administrator can read the log.
- A setting turned off, or a screen the app cannot see, ends the session at
  the last moment the screen was visible.
- No screenshot for two intervals plus a few minutes ends the session even
  while the app keeps checking in — except for time with the screen locked.
- Switching somebody to work from home while punched in ends that session at
  the switch, not at its start.
- Ten minutes with no keyboard or mouse use ends the session at the last use.
"""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today, utc_now
from app.db.mongo import Collections
from tests.conftest import OFFICE_LOCATION, authed_client, invite_and_accept
from tests.test_screenshots import _employee, _punched_in_from_home, _upload

pytestmark = pytest.mark.usefixtures("db", "_storage_in_tmp")

MAC_APP = {
    "X-Client": "desktop",
    "X-App-Version": "1.0.1",
    "X-App-Platform": "darwin-arm64",
    "X-OS-Version": "26.2.0",
}


@pytest.fixture
def _storage_in_tmp(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.utils.storage import reset_storage_cache

    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_STORAGE_DIR", str(tmp_path / "uploads"))
    reset_storage_cache()
    yield
    reset_storage_cache()


async def _log(admin_client, api_prefix, user_id):
    response = await admin_client.get(
        f"{api_prefix}/activity/log", params={"user_id": user_id, "date": str(org_today())}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _move_session_start(db, user_id, at):
    await db[Collections.ATTENDANCE].update_one(
        {"user_id": user_id}, {"$set": {"sessions.0.punch_in.at": at}}
    )


async def test_the_app_log_is_written_by_the_app_and_read_only_by_admins(
    admin_client, api_prefix
):
    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "log@example.com")
    client.headers.update(MAC_APP)
    try:
        now = utc_now()
        sent = await client.post(
            f"{api_prefix}/activity/events",
            json={
                "events": [
                    {"at": now.isoformat(), "kind": "APP_OPENED"},
                    {
                        "at": now.isoformat(),
                        "kind": "SETTING_CHANGED",
                        "details": {"setting": "screen", "state": "denied", "first": False},
                    },
                    {
                        "at": now.isoformat(),
                        "kind": "SCREEN_NOT_VISIBLE",
                        "details": {"seconds": 60, "reported": "on"},
                    },
                    # The server's own kinds are not the app's to claim.
                    {"at": now.isoformat(), "kind": "AUTO_PUNCHED_OUT"},
                    # Nor the future.
                    {"at": (now + timedelta(hours=1)).isoformat(), "kind": "APP_CLOSED"},
                ]
            },
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["data"] == {"accepted": 3, "ignored": 2}

        # The employee cannot read it back.
        own = await client.get(f"{api_prefix}/activity/log", params={"user_id": user_id})
        assert own.status_code == 403
    finally:
        await client.aclose()

    messages = [entry["message"] for entry in await _log(admin_client, api_prefix, user_id)]
    assert "Punched in (website)" in messages
    assert "App opened — 1.0.1, macOS 26.2.0" in messages
    assert "Screen & System Audio Recording: turned off" in messages
    assert (
        "Screen could not be seen for 1 min — macOS reports Screen & System Audio Recording: ON"
        in messages
    )


async def test_a_setting_turned_off_ends_the_session_when_the_screen_was_last_seen(
    admin_client, api_prefix, db
):
    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "off@example.com")
    client.headers.update(MAC_APP)
    started = utc_now() - timedelta(hours=1)
    await _move_session_start(db, user_id, started)
    last_seen = utc_now() - timedelta(minutes=2)
    try:
        stopped = await client.post(
            f"{api_prefix}/screenshots/stopped",
            json={"reason": "SETTING_OFF", "setting": "screen", "at": last_seen.isoformat()},
        )
        assert stopped.status_code == 200, stopped.text
        [session] = stopped.json()["data"]["sessions"]
        assert session["closed_reason"] == "SCREEN_SHARE_STOPPED"
        assert session["closed_detail"] == "SETTING_OFF"
    finally:
        await client.aclose()

    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    ended = record["sessions"][0]
    assert ended["closed_detail"] == "SETTING_OFF"
    assert abs((ended["punch_out"]["at"] - last_seen).total_seconds()) < 1

    notice = await db[Collections.NOTIFICATIONS].find_one({"user_id": user_id})
    assert notice["title"] == "You were punched out"
    assert "Please allow the setting and punch in again." in notice["body"]
    # Nothing about why, for the employee.
    assert "screen" not in notice["body"].lower()

    messages = [entry["message"] for entry in await _log(admin_client, api_prefix, user_id)]
    # Logged when it happened (now), saying where the session ended (2 min ago).
    [auto] = [m for m in messages if m.startswith("Punched out automatically")]
    assert auto.startswith("Punched out automatically — a setting was turned off (session ended ")


async def test_ten_idle_minutes_end_the_session_at_the_last_use(admin_client, api_prefix, db):
    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "idle@example.com")
    client.headers.update(MAC_APP)
    await _move_session_start(db, user_id, utc_now() - timedelta(hours=1))
    last_use = utc_now() - timedelta(minutes=10, seconds=5)
    try:
        stopped = await client.post(
            f"{api_prefix}/screenshots/stopped",
            json={"reason": "IDLE", "at": last_use.isoformat()},
        )
        assert stopped.status_code == 200, stopped.text
        [session] = stopped.json()["data"]["sessions"]
        assert session["closed_detail"] == "IDLE"
        # The ten idle minutes are not worked.
        assert session["duration_minutes"] == 49
    finally:
        await client.aclose()

    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    assert abs((record["sessions"][0]["punch_out"]["at"] - last_use).total_seconds()) < 1

    notice = await db[Collections.NOTIFICATIONS].find_one({"user_id": user_id})
    assert notice["title"] == "You were punched out"
    assert notice["body"].endswith(
        "because your PC was idle for 10 minutes. Please punch in again."
    )

    messages = [entry["message"] for entry in await _log(admin_client, api_prefix, user_id)]
    [auto] = [m for m in messages if m.startswith("Punched out automatically")]
    assert auto.startswith(
        "Punched out automatically — no keyboard or mouse use for 10 min (session ended "
    )


async def test_the_time_away_after_an_automatic_punch_out_says_why(
    admin_client, api_prefix, db
):
    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "gap@example.com")
    await _move_session_start(db, user_id, utc_now() - timedelta(hours=1))
    try:
        stopped = await client.post(
            f"{api_prefix}/screenshots/stopped",
            json={"reason": "IDLE", "at": (utc_now() - timedelta(minutes=30)).isoformat()},
        )
        assert stopped.status_code == 200, stopped.text
        assert (await _upload(client, api_prefix)).status_code == 201
        again = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert again.status_code == 200, again.text
        [gap] = again.json()["data"]["breaks"]
        assert gap["kind"] == "SCREEN_SHARE_STOPPED"
        assert gap["detail"] == "IDLE"
        assert gap["minutes"] == 30
    finally:
        await client.aclose()


async def test_a_last_seen_time_is_never_taken_from_hours_back(admin_client, api_prefix, db):
    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "old@example.com")
    await _move_session_start(db, user_id, utc_now() - timedelta(hours=8))
    try:
        stopped = await client.post(
            f"{api_prefix}/screenshots/stopped",
            json={
                "reason": "SCREEN_NOT_VISIBLE",
                "at": (utc_now() - timedelta(hours=6)).isoformat(),
            },
        )
        assert stopped.status_code == 200, stopped.text
    finally:
        await client.aclose()
    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    ended_at = record["sessions"][0]["punch_out"]["at"]
    assert abs((utc_now() - ended_at) - timedelta(hours=2)).total_seconds() < 60


async def test_no_screenshots_end_the_session_even_while_the_app_checks_in(
    admin_client, api_prefix, db
):
    from app.services.attendance import close_sessions_without_screenshots

    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "noshots@example.com")
    try:
        # Just punched in, with a screenshot: nothing to do.
        assert await close_sessions_without_screenshots() == 0

        # The app kept checking in, but the last screenshot is 40 minutes old
        # (the limit is 2 × 10 + 5 = 25 minutes).
        last_shot = utc_now() - timedelta(minutes=40)
        await _move_session_start(db, user_id, utc_now() - timedelta(hours=3))
        await db[Collections.SCREENSHOTS].update_many(
            {"user_id": user_id}, {"$set": {"captured_at": last_shot}}
        )
        assert (await client.post(f"{api_prefix}/screenshots/heartbeat")).status_code == 200
    finally:
        await client.aclose()

    assert await close_sessions_without_screenshots() == 1
    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    session = record["sessions"][0]
    assert session["closed_detail"] == "NO_SCREENSHOTS"
    # Ended at the last screenshot, not when the job noticed.
    assert abs((session["punch_out"]["at"] - last_shot).total_seconds()) < 1
    assert await close_sessions_without_screenshots() == 0

    messages = [entry["message"] for entry in await _log(admin_client, api_prefix, user_id)]
    assert any(
        m.startswith("Punched out automatically — no screenshot arrived for 25 min (session ended ")
        for m in messages
    )


async def test_a_locked_screen_is_not_held_against_missing_screenshots(
    admin_client, api_prefix, db
):
    from app.services.attendance import close_sessions_without_screenshots

    client, user_id = await _punched_in_from_home(admin_client, api_prefix, "lunch@example.com")
    await client.aclose()
    now = utc_now().replace(second=0, microsecond=0)
    last_shot = now - timedelta(minutes=40)
    await _move_session_start(db, user_id, now - timedelta(hours=3))
    await db[Collections.SCREENSHOTS].update_many(
        {"user_id": user_id}, {"$set": {"captured_at": last_shot}}
    )
    # Locked for 30 of those 40 minutes: 10 minutes without a screenshot.
    await db[Collections.ACTIVITY].insert_many(
        [
            {
                "user_id": user_id,
                "date": date_key(org_today()),
                "minute": last_shot + timedelta(minutes=5 + i),
                "locked_seconds": 60,
            }
            for i in range(30)
        ]
    )
    assert await close_sessions_without_screenshots() == 0

    # The lock shows in the admin's log as one line.
    entries = await _log(admin_client, api_prefix, user_id)
    locked = [entry for entry in entries if entry["kind"] == "SCREEN_LOCKED"]
    assert [entry["message"] for entry in locked] == ["Screen locked (30 min)"]
    assert locked[0]["until"] is not None


async def test_switching_to_home_while_punched_in_ends_the_session_at_the_switch(
    admin_client, api_prefix, db
):
    session = await invite_and_accept(admin_client, api_prefix, email="switch@example.com")
    user_id = session["user"]["id"]
    office = authed_client(session["tokens"]["access_token"])
    try:
        punched = await office.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert punched.status_code == 200, punched.text
    finally:
        await office.aclose()
    await _move_session_start(db, user_id, utc_now() - timedelta(minutes=30))

    switched = await admin_client.patch(f"{api_prefix}/users/{user_id}", json={"work_mode": "WFH"})
    assert switched.status_code == 200, switched.text

    record = await db[Collections.ATTENDANCE].find_one({"user_id": user_id})
    ended = record["sessions"][0]
    assert ended["closed_detail"] == "WORK_MODE_CHANGED"
    # At the switch: the 30 minutes before it count.
    assert abs((utc_now() - ended["punch_out"]["at"]).total_seconds()) < 30
    assert record["worked_minutes"] == 30


async def test_an_app_update_is_logged(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "upd@example.com", wfh=True)
    try:
        client.headers.update({**MAC_APP, "X-App-Version": "1.0.0"})
        assert (await client.get(f"{api_prefix}/attendance/today")).status_code == 200
        client.headers["X-App-Version"] = "1.0.1"
        assert (await client.get(f"{api_prefix}/attendance/today")).status_code == 200
        assert (await _upload(client, api_prefix)).status_code == 201
    finally:
        await client.aclose()

    messages = [entry["message"] for entry in await _log(admin_client, api_prefix, user_id)]
    assert "App updated 1.0.0 → 1.0.1" in messages
