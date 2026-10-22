"""Device classification — the gate that keeps punching off phones.

The user agents below are real strings, not invented ones.
"""

import json

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.utils.device import (
    UNKNOWN_PLATFORM,
    DeviceClass,
    DeviceProfile,
    classify,
    platform_name,
)

DESKTOP_PROFILE = json.dumps(
    {"maxTouchPoints": 0, "pointerCoarse": False, "screenWidth": 1920, "screenHeight": 1080,
     "platform": "Win32"}
)
TOUCH_LAPTOP_PROFILE = json.dumps(
    {"maxTouchPoints": 10, "pointerCoarse": False, "screenWidth": 1920, "screenHeight": 1080,
     "platform": "Win32"}
)
PHONE_PROFILE = json.dumps(
    {"maxTouchPoints": 5, "pointerCoarse": True, "screenWidth": 390, "screenHeight": 844,
     "platform": "iPhone"}
)
IPAD_DESKTOP_MODE_PROFILE = json.dumps(
    {"maxTouchPoints": 5, "pointerCoarse": True, "screenWidth": 1024, "screenHeight": 1366,
     "platform": "MacIntel"}
)

CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.2 Safari/605.1.15"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
SAFARI_IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.2 Mobile/15E148 Safari/604.1"
)
SAMSUNG_TABLET = (
    "Mozilla/5.0 (Linux; Android 13; SM-X700) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Tablet"
)


def make_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/attendance/punch-in",
        "headers": Headers(headers).raw,
        "query_string": b"",
        "client": ("127.0.0.1", 5000),
    }
    return Request(scope)


# --------------------------------------------------------------------------- #
# desktops get through
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "user_agent", [CHROME_WINDOWS, SAFARI_MAC, FIREFOX_LINUX], ids=["chrome", "safari", "firefox"]
)
def test_a_real_computer_is_allowed(user_agent):
    verdict = classify(
        make_request({"user-agent": user_agent, "x-device-profile": DESKTOP_PROFILE})
    )

    assert verdict.device_class is DeviceClass.DESKTOP
    assert verdict.is_desktop


def test_a_touch_screen_laptop_is_still_a_computer():
    """A Windows laptop with a touch screen has a trackpad, so the pointer is fine."""
    verdict = classify(
        make_request({"user-agent": CHROME_WINDOWS, "x-device-profile": TOUCH_LAPTOP_PROFILE})
    )

    assert verdict.device_class is DeviceClass.DESKTOP


def test_desktop_client_hints_are_honoured():
    verdict = classify(
        make_request(
            {
                "user-agent": CHROME_WINDOWS,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "x-device-profile": DESKTOP_PROFILE,
            }
        )
    )

    assert verdict.device_class is DeviceClass.DESKTOP


# --------------------------------------------------------------------------- #
# phones and tablets are stopped
# --------------------------------------------------------------------------- #
def test_the_mobile_client_hint_is_conclusive():
    verdict = classify(
        make_request(
            {
                "user-agent": CHROME_WINDOWS,  # lying
                "sec-ch-ua-mobile": "?1",
                "x-device-profile": DESKTOP_PROFILE,
            }
        )
    )

    assert verdict.device_class is DeviceClass.MOBILE
    assert "client hint" in verdict.reason


@pytest.mark.parametrize("platform", ['"Android"', '"iOS"'])
def test_a_phone_platform_hint_is_blocked(platform):
    verdict = classify(
        make_request({"user-agent": CHROME_WINDOWS, "sec-ch-ua-platform": platform})
    )

    assert verdict.device_class is DeviceClass.MOBILE


def test_an_iphone_is_blocked_without_any_client_hints():
    verdict = classify(
        make_request({"user-agent": SAFARI_IPHONE, "x-device-profile": PHONE_PROFILE})
    )

    assert verdict.device_class is DeviceClass.MOBILE


def test_an_android_phone_is_blocked():
    verdict = classify(make_request({"user-agent": CHROME_ANDROID}))

    assert verdict.device_class is DeviceClass.MOBILE


@pytest.mark.parametrize("user_agent", [SAFARI_IPAD, SAMSUNG_TABLET], ids=["ipad", "samsung"])
def test_tablets_are_blocked(user_agent):
    verdict = classify(make_request({"user-agent": user_agent}))

    assert verdict.device_class is DeviceClass.TABLET


def test_an_ipad_pretending_to_be_a_mac_is_caught_by_its_touch_screen():
    """Desktop-mode Safari on iPad sends a Mac user agent. A Mac has no touch screen."""
    verdict = classify(
        make_request(
            {"user-agent": SAFARI_MAC, "x-device-profile": IPAD_DESKTOP_MODE_PROFILE}
        )
    )

    assert verdict.device_class is DeviceClass.TABLET
    assert "touch screen" in verdict.reason


def test_a_touch_only_device_is_blocked_even_with_a_desktop_user_agent():
    verdict = classify(
        make_request({"user-agent": FIREFOX_LINUX, "x-device-profile": PHONE_PROFILE})
    )

    assert verdict.device_class is DeviceClass.MOBILE


def test_a_small_touch_screen_is_blocked():
    profile = json.dumps(
        {"maxTouchPoints": 5, "pointerCoarse": False, "screenWidth": 412, "screenHeight": 915}
    )
    verdict = classify(make_request({"user-agent": CHROME_WINDOWS, "x-device-profile": profile}))

    assert verdict.device_class is DeviceClass.MOBILE


# --------------------------------------------------------------------------- #
# missing or malformed evidence
# --------------------------------------------------------------------------- #
def test_a_request_without_a_device_profile_is_not_trusted():
    verdict = classify(make_request({"user-agent": CHROME_WINDOWS}))

    assert verdict.device_class is DeviceClass.UNKNOWN
    assert not verdict.is_desktop


def test_a_request_with_no_headers_at_all_is_not_trusted():
    verdict = classify(make_request({}))

    assert verdict.device_class is DeviceClass.UNKNOWN


@pytest.mark.parametrize("raw", ["not json", "[]", '{"maxTouchPoints": "many"}', ""])
def test_a_malformed_device_profile_is_discarded_rather_than_crashing(raw):
    assert DeviceProfile.parse(raw) is None


def test_a_well_formed_profile_is_parsed():
    profile = DeviceProfile.parse(DESKTOP_PROFILE)

    assert profile is not None
    assert profile.screen_width == 1920
    assert profile.max_touch_points == 0


# --------------------------------------------------------------------------- #
# which operating system — recorded on every punch, never used to block one
# --------------------------------------------------------------------------- #
def test_the_platform_client_hint_is_taken_at_its_word():
    request = make_request({"user-agent": FIREFOX_LINUX, "sec-ch-ua-platform": '"Windows"'})

    assert platform_name(request) == "Windows"


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (CHROME_WINDOWS, "Windows"),
        (SAFARI_MAC, "macOS"),
        (FIREFOX_LINUX, "Linux"),
        (SAFARI_IPHONE, "iOS"),
        (SAFARI_IPAD, "iOS"),
        (CHROME_ANDROID, "Android"),
    ],
    ids=["windows", "mac", "linux", "iphone", "ipad", "android"],
)
def test_the_operating_system_is_read_from_the_user_agent(user_agent, expected):
    """An Android UA also says "Linux" and an iPhone one says "Mac OS X"."""
    assert platform_name(make_request({"user-agent": user_agent})) == expected


def test_navigator_platform_answers_when_the_user_agent_says_nothing():
    profile = json.dumps({"maxTouchPoints": 0, "platform": "MacIntel"})

    assert platform_name(make_request({"x-device-profile": profile})) == "macOS"


def test_a_request_that_reveals_nothing_has_an_unknown_platform():
    assert platform_name(make_request({})) == UNKNOWN_PLATFORM


def test_the_verdict_carries_the_platform_alongside_the_device_class():
    verdict = classify(
        make_request({"user-agent": CHROME_WINDOWS, "x-device-profile": DESKTOP_PROFILE})
    )

    assert verdict.device_class is DeviceClass.DESKTOP
    assert verdict.platform == "Windows"
    assert verdict.user_agent == CHROME_WINDOWS
