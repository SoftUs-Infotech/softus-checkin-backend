"""Desktop-only enforcement for punch in / punch out.

Attendance is a desk activity. This module decides whether a request came from a
computer, and the ``DesktopOnly`` dependency is applied to the two punch routes
only — everything else stays usable on a phone, so an employee can still check
their calendar on the way home.

Four signals, cheapest and most reliable first:

1. ``Sec-CH-UA-Mobile: ?1`` — Chromium's own answer. Trusted when present.
2. ``Sec-CH-UA-Platform`` naming a phone OS.
3. The User-Agent string, for Safari and Firefox which send no client hints.
4. ``X-Device-Profile`` — a required header the frontend fills from
   ``navigator.maxTouchPoints``, ``matchMedia('(pointer: coarse)')`` and screen
   size. This is what catches an iPad in desktop mode, which lies in its UA but
   cannot hide having a touch screen and no mouse.

Every verdict also carries the operating system and the raw user agent, which
are recorded on the punch: the device class decides whether the punch is allowed,
the platform is what an administrator looks at afterwards when one looks wrong.

Honest limitation: someone who opens devtools and forges a desktop UA plus a
plausible device profile will get through. This stops casual punching from a
phone, which is the behaviour being prevented. The office IP allowlist in
:mod:`app.utils.network` is the harder gate that sits alongside it.
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Request

from app.core.exceptions import PermissionDeniedError
from app.core.logging import get_logger

logger = get_logger(__name__)

DEVICE_PROFILE_HEADER = "X-Device-Profile"

# Phones and tablets, in the order they are worth checking.
_MOBILE_UA = re.compile(
    r"(android|iphone|ipod|ipad|windows phone|iemobile|blackberry|bb10|"
    r"opera mini|opera mobi|mobile safari|fennec|silk|kindle|playbook|"
    r"webos|palm|symbian|nokia|tizen|kaios)",
    re.IGNORECASE,
)
_TABLET_UA = re.compile(r"(ipad|tablet|playbook|silk|kindle|nexus 7|nexus 10)", re.IGNORECASE)
# "Mobile" appears in some desktop UAs; require a real hint alongside it.
_GENERIC_MOBILE_TOKEN = re.compile(r"\bMobi\b|\bMobile\b", re.IGNORECASE)

_MOBILE_PLATFORMS = {"android", "ios"}

#: ``Sec-CH-UA-Platform`` values, normalised to what an admin wants to read in
#: the register.
_PLATFORM_NAMES = {
    "windows": "Windows",
    "macos": "macOS",
    "android": "Android",
    "ios": "iOS",
    "linux": "Linux",
    "chrome os": "Chrome OS",
    "chromium os": "Chrome OS",
}

# Order matters: an Android user agent also says "Linux", and an iPhone one also
# says "Mac OS X", so the specific patterns have to be tried first.
_PLATFORM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"iphone|ipad|ipod|ios ", re.IGNORECASE), "iOS"),
    (re.compile(r"android", re.IGNORECASE), "Android"),
    (re.compile(r"cros", re.IGNORECASE), "Chrome OS"),
    (re.compile(r"windows", re.IGNORECASE), "Windows"),
    (re.compile(r"mac os x|macintosh", re.IGNORECASE), "macOS"),
    (re.compile(r"linux|x11", re.IGNORECASE), "Linux"),
)

# ``navigator.platform``, the last resort.
_PROFILE_PLATFORM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"iphone|ipad|ipod", re.IGNORECASE), "iOS"),
    (re.compile(r"android", re.IGNORECASE), "Android"),
    (re.compile(r"win", re.IGNORECASE), "Windows"),
    (re.compile(r"mac", re.IGNORECASE), "macOS"),
    (re.compile(r"cros", re.IGNORECASE), "Chrome OS"),
    (re.compile(r"linux", re.IGNORECASE), "Linux"),
)

UNKNOWN_PLATFORM = "Unknown"

# Below this, a touch-only screen is a phone rather than a small laptop.
_MIN_DESKTOP_WIDTH = 900


class DeviceClass(StrEnum):
    DESKTOP = "DESKTOP"
    MOBILE = "MOBILE"
    TABLET = "TABLET"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """What the browser reported about itself."""

    max_touch_points: int = 0
    pointer_coarse: bool = False
    screen_width: int = 0
    screen_height: int = 0
    platform: str = ""

    @classmethod
    def parse(cls, raw: str | None) -> "DeviceProfile | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return cls(
                max_touch_points=int(data.get("maxTouchPoints", 0) or 0),
                pointer_coarse=bool(data.get("pointerCoarse", False)),
                screen_width=int(data.get("screenWidth", 0) or 0),
                screen_height=int(data.get("screenHeight", 0) or 0),
                platform=str(data.get("platform", "") or "")[:60],
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class DeviceVerdict:
    device_class: DeviceClass
    reason: str
    user_agent: str
    profile: DeviceProfile | None
    platform: str = UNKNOWN_PLATFORM

    @property
    def is_desktop(self) -> bool:
        return self.device_class is DeviceClass.DESKTOP


def _hint(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip().strip('"').lower()


def platform_name(request: Request, profile: DeviceProfile | None = None) -> str:
    """The operating system behind a request, in a form worth showing an admin.

    Recorded on every punch: "phone or laptop" answers whether the punch was
    allowed, but "which machine, which OS" is what an administrator actually
    asks when a punch looks wrong.
    """
    hinted = _PLATFORM_NAMES.get(_hint(request, "sec-ch-ua-platform"))
    if hinted:
        return hinted

    user_agent = request.headers.get("user-agent", "")
    for pattern, name in _PLATFORM_PATTERNS:
        if pattern.search(user_agent):
            return name

    if profile is None:
        profile = DeviceProfile.parse(request.headers.get(DEVICE_PROFILE_HEADER))
    if profile and profile.platform:
        for pattern, name in _PROFILE_PLATFORM_PATTERNS:
            if pattern.search(profile.platform):
                return name

    return UNKNOWN_PLATFORM


def classify(request: Request) -> DeviceVerdict:
    """Classify the device behind a request. Never raises."""
    user_agent = request.headers.get("user-agent", "")
    profile = DeviceProfile.parse(request.headers.get(DEVICE_PROFILE_HEADER))
    platform = platform_name(request, profile)

    def verdict(device_class: DeviceClass, reason: str) -> DeviceVerdict:
        return DeviceVerdict(device_class, reason, user_agent, profile, platform)

    # 1. Client hints — the browser's own answer.
    if _hint(request, "sec-ch-ua-mobile") == "?1":
        return verdict(DeviceClass.MOBILE, "client hint reports a mobile device")

    hinted_platform = _hint(request, "sec-ch-ua-platform")
    if hinted_platform in _MOBILE_PLATFORMS:
        return verdict(
            DeviceClass.MOBILE, f"client hint reports platform '{hinted_platform}'"
        )

    # 2. User-Agent, for browsers that send no hints.
    if _TABLET_UA.search(user_agent):
        return verdict(DeviceClass.TABLET, "user agent identifies a tablet")
    if _MOBILE_UA.search(user_agent):
        return verdict(DeviceClass.MOBILE, "user agent identifies a mobile device")

    # 3. The device profile catches what the UA hides.
    if profile is None:
        return verdict(DeviceClass.UNKNOWN, "the browser did not report a device profile")

    # An iPad in desktop mode claims to be a Mac, but a Mac has no touch screen.
    if "mac" in user_agent.lower() and profile.max_touch_points > 1:
        return verdict(
            DeviceClass.TABLET,
            "a touch screen was detected on a device claiming to be a desktop Mac",
        )

    # Touch and no fine pointer means no mouse or trackpad.
    if profile.pointer_coarse and profile.max_touch_points > 0:
        return verdict(
            DeviceClass.MOBILE, "the device has a touch screen and no mouse or trackpad"
        )

    shortest_edge = min(
        [value for value in (profile.screen_width, profile.screen_height) if value] or [0]
    )
    if shortest_edge and shortest_edge < _MIN_DESKTOP_WIDTH and profile.max_touch_points > 0:
        return verdict(
            DeviceClass.MOBILE,
            "the screen is too small for a computer and the device has a touch screen",
        )

    if _GENERIC_MOBILE_TOKEN.search(user_agent) and profile.max_touch_points > 0:
        return verdict(DeviceClass.MOBILE, "the user agent carries a mobile token")

    return verdict(DeviceClass.DESKTOP, "desktop or laptop")


async def require_desktop(request: Request) -> DeviceVerdict:
    """Dependency for the punch routes. Blocks anything that is not a computer.

    Honours the ``block_mobile_punch`` setting so an administrator can turn the
    restriction off without a deployment.
    """
    from app.services import settings as settings_service

    verdict = classify(request)
    org_settings = await settings_service.get_settings()

    if not org_settings.get("block_mobile_punch", True):
        return verdict

    if verdict.is_desktop:
        return verdict

    logger.info(
        "Blocked a punch attempt from %s (%s) ua=%r",
        verdict.device_class,
        verdict.reason,
        verdict.user_agent[:200],
    )
    raise PermissionDeniedError(
        "Punching in and out must be done from a laptop or desktop computer. "
        "This device was identified as a phone or tablet.",
        code="MOBILE_DEVICE_BLOCKED",
        details={"device_class": verdict.device_class.value, "reason": verdict.reason},
    )


DesktopOnly = Annotated[DeviceVerdict, Depends(require_desktop)]
