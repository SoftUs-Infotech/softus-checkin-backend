"""The desktop app: which version people run, releases and forced updates,
the desktop-only rule for working from home, and activity tracking."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.datetime import utc_now
from app.services import app_client
from app.utils.client import version_key
from app.utils.storage import reset_storage_cache
from tests.conftest import DESKTOP_HEADERS, OFFICE_LOCATION, authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256
ZIP = b"PK\x03\x04" + b"\x00" * 2048
APPIMAGE = b"\x7fELF" + b"\x00" * 2048
DMG = b"\x00" * 2048 + b"koly" + b"\x00" * 508


def desktop(version: str = "1.0.0", platform: str = "darwin-arm64") -> dict[str, str]:
    return {
        "X-Client": "desktop",
        "X-App-Version": version,
        "X-App-Platform": platform,
        "X-OS-Version": "14.5",
    }


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_STORAGE_DIR", str(tmp_path / "uploads"))
    reset_storage_cache()
    app_client.reset()
    yield tmp_path / "uploads"
    reset_storage_cache()
    app_client.reset()


async def _employee(admin_client, api_prefix, email: str, *, wfh: bool):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    user_id = session["user"]["id"]
    if wfh:
        response = await admin_client.patch(
            f"{api_prefix}/users/{user_id}", json={"work_mode": "WFH"}
        )
        assert response.status_code == 200, response.text
    return authed_client(session["tokens"]["access_token"]), user_id


async def _upload_release(admin_client, api_prefix, version, platform, name, data, arch=None):
    form = {"version": version, "platform": platform}
    if arch:
        form["arch"] = arch
    return await admin_client.post(
        f"{api_prefix}/app-releases/upload",
        data=form,
        files={"file": (name, data, "application/octet-stream")},
    )


async def _publish_mac(admin_client, api_prefix, version="1.0.1"):
    response = await _upload_release(
        admin_client, api_prefix, version, "mac", f"softus-checkin-{version}-mac-arm64.zip", ZIP
    )
    assert response.status_code == 201, response.text
    release_id = response.json()["data"]["id"]
    published = await admin_client.post(f"{api_prefix}/app-releases/{release_id}/publish")
    assert published.status_code == 200, published.text
    return release_id


# --------------------------------------------------------------------------- #
# which app people use
# --------------------------------------------------------------------------- #
def test_versions_sort_like_people_expect():
    assert version_key("1.0.10") > version_key("1.0.9")
    assert version_key("1.1.0") > version_key("1.0.99")
    assert version_key("1.0.0") > version_key("1.0.0-beta.2")
    assert version_key("0.0.1") > version_key(None) == version_key("garbage")


async def test_the_admin_sees_which_app_and_version_each_person_uses(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "mac@example.com", wfh=True)
    async with client:
        assert (await client.get(f"{api_prefix}/attendance/today")).status_code == 200
        before = (await admin_client.get(f"{api_prefix}/users/{user_id}")).json()["data"]
        assert before["desktop_app"] is None and before["web_last_seen_at"] is not None

        client.headers.update(desktop("1.0.3", "darwin-arm64"))
        assert (await client.get(f"{api_prefix}/attendance/today")).status_code == 200

    after = (await admin_client.get(f"{api_prefix}/users/{user_id}")).json()["data"]
    assert after["desktop_app"]["version"] == "1.0.3"
    assert after["desktop_app"]["os"] == "darwin" and after["desktop_app"]["arch"] == "arm64"
    assert after["desktop_app"]["os_version"] == "14.5"
    listed = (await admin_client.get(f"{api_prefix}/users", params={"page_size": 50})).json()
    row = next(u for u in listed["data"]["items"] if u["id"] == user_id)
    assert row["desktop_app"]["version"] == "1.0.3"


# --------------------------------------------------------------------------- #
# releases
# --------------------------------------------------------------------------- #
async def test_a_release_is_uploaded_as_a_draft_and_published(admin_client, api_prefix, _isolated):
    dmg = await _upload_release(
        admin_client, api_prefix, "1.0.1", "mac", "softus-checkin-1.0.1-mac-arm64.dmg", DMG
    )
    assert dmg.status_code == 201, dmg.text
    release = dmg.json()["data"]
    assert release["status"] == "DRAFT" and release["updatable"] is False
    assert release["files"][0]["arch"] == "arm64" and release["files"][0]["kind"] == "dmg"

    # Without the .zip the app could not update itself.
    refused = await admin_client.post(f"{api_prefix}/app-releases/{release['id']}/publish")
    assert refused.json()["error"]["code"] == "RELEASE_INCOMPLETE"

    zipped = await _upload_release(
        admin_client, api_prefix, "1.0.1", "mac", "softus-checkin-1.0.1-mac-arm64.zip", ZIP
    )
    assert zipped.json()["data"]["id"] == release["id"]
    assert {f["kind"] for f in zipped.json()["data"]["files"]} == {"dmg", "zip"}

    published = await admin_client.post(f"{api_prefix}/app-releases/{release['id']}/publish")
    assert published.status_code == 200 and published.json()["data"]["status"] == "PUBLISHED"
    assert (await admin_client.get(f"{api_prefix}/app-releases/published")).json()["data"] == {
        "mac": "1.0.1",
        "linux": None,
    }
    # Published files are on disk where the release says.
    assert (_isolated / "releases/mac/1.0.1/softus-checkin-1.0.1-mac-arm64.zip").read_bytes() == ZIP

    # A published version is final; an older or equal one cannot be published over it.
    again = await _upload_release(
        admin_client, api_prefix, "1.0.1", "mac", "softus-checkin-1.0.1-mac-arm64.zip", ZIP
    )
    assert again.json()["error"]["code"] == "RELEASE_NOT_DRAFT"
    older = await _upload_release(
        admin_client, api_prefix, "1.0.0", "mac", "softus-checkin-1.0.0-mac-arm64.zip", ZIP
    )
    older_publish = await admin_client.post(
        f"{api_prefix}/app-releases/{older.json()['data']['id']}/publish"
    )
    assert older_publish.json()["error"]["code"] == "RELEASE_NOT_NEWER"


async def test_upload_checks_catch_the_wrong_file(admin_client, api_prefix):
    wrong_version = await _upload_release(
        admin_client, api_prefix, "1.0.2", "mac", "softus-checkin-1.0.1-mac-arm64.zip", ZIP
    )
    assert wrong_version.json()["error"]["code"] == "VERSION_MISMATCH"
    wrong_platform = await _upload_release(
        admin_client, api_prefix, "1.0.2", "linux", "softus-checkin-1.0.2-mac-arm64.zip", ZIP
    )
    assert wrong_platform.json()["error"]["code"] == "WRONG_FILE_FOR_PLATFORM"
    not_really = await _upload_release(
        admin_client, api_prefix, "1.0.2", "linux", "softus-checkin-1.0.2-linux-x64.AppImage", ZIP
    )
    assert not_really.json()["error"]["code"] == "NOT_A_RELEASE_FILE"
    no_arch = await _upload_release(
        admin_client, api_prefix, "1.0.2", "linux", "app.AppImage", APPIMAGE
    )
    assert no_arch.json()["error"]["code"] == "ARCH_REQUIRED"
    given = await _upload_release(
        admin_client, api_prefix, "1.0.2", "linux", "app.AppImage", APPIMAGE, arch="x64"
    )
    assert given.status_code == 201, given.text


async def test_only_admins_upload_but_anyone_can_ask_for_the_latest(
    admin_client, api_prefix, client
):
    employee, _ = await _employee(admin_client, api_prefix, "plain@example.com", wfh=False)
    async with employee:
        refused = await employee.post(
            f"{api_prefix}/app-releases/upload",
            data={"version": "1.0.1", "platform": "mac"},
            files={"file": ("softus-checkin-1.0.1-mac-arm64.zip", ZIP, "application/zip")},
        )
        assert refused.status_code == 403

        assert (
            await employee.get(f"{api_prefix}/app-releases/latest", params={"platform": "mac"})
        ).json()["data"] is None
        await _publish_mac(admin_client, api_prefix, "1.0.1")
        latest = (
            await employee.get(
                f"{api_prefix}/app-releases/latest", params={"platform": "mac", "arch": "arm64"}
            )
        ).json()["data"]
        assert latest["version"] == "1.0.1"
        assert latest["files"][0]["url"].startswith("/files?") and latest["files"][0]["sha256"]
        # Nothing published for Intel Macs yet.
        intel = await employee.get(
            f"{api_prefix}/app-releases/latest", params={"platform": "mac", "arch": "x64"}
        )
        assert intel.json()["data"] is None

    # The download link works without a sign-in, and is named for saving.
    download = await client.get(f"{api_prefix}{latest['files'][0]['url']}")
    assert download.status_code == 200 and download.content == ZIP
    assert "softus-checkin-1.0.1-mac-arm64.zip" in download.headers["content-disposition"]


async def test_a_release_is_withdrawn_before_it_can_be_deleted(admin_client, api_prefix, _isolated):
    release_id = await _publish_mac(admin_client, api_prefix, "1.0.1")
    refused = await admin_client.delete(f"{api_prefix}/app-releases/{release_id}")
    assert refused.json()["error"]["code"] == "RELEASE_PUBLISHED"
    withdrawn = await admin_client.post(f"{api_prefix}/app-releases/{release_id}/withdraw")
    assert withdrawn.json()["data"]["status"] == "WITHDRAWN"
    assert (await admin_client.get(f"{api_prefix}/app-releases/published")).json()["data"][
        "mac"
    ] is None
    deleted = await admin_client.delete(f"{api_prefix}/app-releases/{release_id}")
    assert deleted.status_code == 200
    assert not (_isolated / "releases/mac/1.0.1/softus-checkin-1.0.1-mac-arm64.zip").exists()


# --------------------------------------------------------------------------- #
# punching in from the app
# --------------------------------------------------------------------------- #
async def test_an_outdated_app_must_update_before_punching_in(admin_client, api_prefix):
    await _publish_mac(admin_client, api_prefix, "1.0.1")
    client, _ = await _employee(admin_client, api_prefix, "old@example.com", wfh=True)
    async with client:
        client.headers.update(desktop("1.0.0", "darwin-arm64"))
        await client.post(
            f"{api_prefix}/screenshots", files={"file": ("s.jpg", JPEG, "image/jpeg")}
        )
        refused = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert refused.status_code == 409
        assert refused.json()["error"] == {
            "code": "APP_UPDATE_REQUIRED",
            "details": {"version": "1.0.1"},
        }

        # Updated: the same person punches in.
        client.headers.update(desktop("1.0.1", "darwin-arm64"))
        current = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert current.status_code == 200, current.text

    # An Intel Mac has no 1.0.1 to install, so it is not held back.
    intel_client, _ = await _employee(admin_client, api_prefix, "intel@example.com", wfh=True)
    async with intel_client:
        intel_client.headers.update(desktop("1.0.0", "darwin-x64"))
        await intel_client.post(
            f"{api_prefix}/screenshots", files={"file": ("s.jpg", JPEG, "image/jpeg")}
        )
        intel = await intel_client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert intel.status_code == 200, intel.text


async def test_the_organisation_can_require_the_desktop_app_for_working_from_home(
    admin_client, api_prefix
):
    home, _ = await _employee(admin_client, api_prefix, "home@example.com", wfh=True)
    office, _ = await _employee(admin_client, api_prefix, "office@example.com", wfh=False)
    switched = await admin_client.patch(
        f"{api_prefix}/settings", json={"wfh_desktop_app_required": True}
    )
    assert switched.status_code == 200, switched.text
    async with home, office:
        today = (await home.get(f"{api_prefix}/attendance/today")).json()["data"]
        assert today["desktop_app_required"] is True
        await home.post(f"{api_prefix}/screenshots", files={"file": ("s.jpg", JPEG, "image/jpeg")})
        refused = await home.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "DESKTOP_APP_REQUIRED"
        assert "home" not in refused.json()["message"].lower()

        home.headers.update(desktop("1.0.0"))
        allowed = await home.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert allowed.status_code == 200, allowed.text

        # Office staff keep using the website.
        assert (await office.get(f"{api_prefix}/attendance/today")).json()["data"][
            "desktop_app_required"
        ] is False
        desk = await office.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        assert desk.status_code == 200, desk.text


async def test_the_linux_app_punches_without_a_location(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "tux@example.com", wfh=True)
    async with client:
        await client.post(
            f"{api_prefix}/screenshots", files={"file": ("s.jpg", JPEG, "image/jpeg")}
        )
        browser = await client.post(f"{api_prefix}/attendance/punch-in", json={})
        assert browser.json()["error"]["code"] == "LOCATION_REQUIRED"
        client.headers.update(desktop("1.0.0", "linux-x64"))
        linux = await client.post(f"{api_prefix}/attendance/punch-in", json={})
        assert linux.status_code == 200, linux.text


# --------------------------------------------------------------------------- #
# activity
# --------------------------------------------------------------------------- #
def _minute(at: datetime, **figures) -> dict:
    return {"minute": at.isoformat(), "active_seconds": 0, **figures}


async def test_activity_is_stored_per_minute_and_read_back_per_window(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "busy@example.com", wfh=True)
    interval = 10
    now = utc_now()
    # The start of the window two windows back, so all four windows are in the past.
    stamp = int(now.timestamp())
    base = datetime.fromtimestamp(stamp - stamp % (interval * 60) - 4 * interval * 60, tz=UTC)

    def window(index: int, **figures) -> list[dict]:
        start = base + timedelta(minutes=interval * index)
        return [_minute(start + timedelta(minutes=m), **figures) for m in range(interval)]

    minutes = (
        window(
            0,
            active_seconds=50,
            keys=80,
            clicks=10,
            mouse_distance=4000,
            screen_samples=4,
            screen_changes=3,
        )
        + window(1, active_seconds=2)  # nobody there
        + window(
            2, active_seconds=55, mouse_distance=900, screen_samples=4, screen_changes=0
        )  # jiggler
        + window(3, active_seconds=0, screen_samples=4, screen_changes=4)  # a video
    )
    async with client:
        client.headers.update(desktop("1.0.0"))
        saved = await client.post(f"{api_prefix}/activity", json={"minutes": minutes})
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"] == {"accepted": 40, "ignored": 0}
        # Sending the same minutes again replaces, it does not add.
        again = await client.post(f"{api_prefix}/activity", json={"minutes": minutes[:5]})
        assert again.json()["data"]["accepted"] == 5
        # Too old, or from the future: ignored.
        odd = await client.post(
            f"{api_prefix}/activity",
            json={"minutes": [_minute(now - timedelta(days=3)), _minute(now + timedelta(hours=1))]},
        )
        assert odd.json()["data"] == {"accepted": 0, "ignored": 2}
        # Employees cannot read activity.
        assert (
            await client.get(f"{api_prefix}/activity/day", params={"user_id": user_id})
        ).status_code == 403

    result = (
        await admin_client.get(f"{api_prefix}/activity/day", params={"user_id": user_id})
    ).json()["data"]
    windows = result["windows"]
    if len(windows) < 4:
        pytest.skip("These windows straddle midnight in the organisation's timezone.")
    busy, idle, jiggler, video = windows[-4:]
    assert busy["tracked_minutes"] == 10 and busy["keys"] == 800 and busy["level"] == "high"
    assert busy["active_percent"] == 83 and busy["flags"] == []
    assert idle["flags"] == ["idle"] and idle["level"] == "low"
    assert "mouse_only" in jiggler["flags"]
    assert "screen_only" in video["flags"] and "idle" in video["flags"]
    assert result["totals"]["tracked_minutes"] == 40


async def test_office_staff_send_no_activity(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, "desk2@example.com", wfh=False)
    async with client:
        refused = await client.post(
            f"{api_prefix}/activity", json={"minutes": [_minute(utc_now(), active_seconds=10)]}
        )
    assert refused.status_code == 403


def test_desktop_headers_fixture_is_a_browser():
    # The shared fixture headers describe a browser; desktop tests add their own.
    assert "X-Client" not in DESKTOP_HEADERS and "x-client" not in DESKTOP_HEADERS


async def test_settings_expose_the_desktop_switch(admin_client, api_prefix):
    data = (await admin_client.get(f"{api_prefix}/settings")).json()["data"]
    assert data["wfh_desktop_app_required"] is False


async def test_input_from_software_devices_is_flagged(admin_client, api_prefix):
    client, user_id = await _employee(admin_client, api_prefix, "remote@example.com", wfh=True)
    stamp = int(utc_now().timestamp())
    start = datetime.fromtimestamp(stamp - stamp % 600 - 1200, tz=UTC)
    minutes = [
        _minute(
            start + timedelta(minutes=m),
            active_seconds=50,
            keys=40,
            clicks=5,
            mouse_distance=800,
            screen_samples=4,
            screen_changes=2,
            virtual_seconds=45,
        )
        for m in range(10)
    ]
    async with client:
        client.headers.update(desktop("1.0.0", "linux-x64"))
        saved = await client.post(f"{api_prefix}/activity", json={"minutes": minutes})
        assert saved.status_code == 200, saved.text
    result = (
        await admin_client.get(f"{api_prefix}/activity/day", params={"user_id": user_id})
    ).json()["data"]
    if not result["windows"]:
        pytest.skip("The window falls on yesterday in the organisation's timezone.")
    window = result["windows"][-1]
    assert window["virtual_seconds"] == 450
    assert "virtual_input" in window["flags"]
