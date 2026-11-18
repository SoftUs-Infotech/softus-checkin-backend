"""Which app a request came from: the desktop app (and which build), or a browser.

The desktop app adds these headers to every request it makes:

* ``X-Client: desktop``
* ``X-App-Version: 1.0.3`` — the app's own version
* ``X-App-Platform: darwin-arm64`` — Node's ``process.platform``-``process.arch``
* ``X-OS-Version: 14.5`` — the operating system's version, for the admin to read

A browser sends none of them, so anything without ``X-Client: desktop`` is the
website. Like the device profile, this is a report rather than a proof: the
point is that a work-from-home punch arrives through the app that tracks
activity, and that the app is the published version — not to stop somebody
determined to forge headers, who would still need the app's screenshots and
activity to look like a working day.
"""

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

CLIENT_HEADER = "X-Client"
VERSION_HEADER = "X-App-Version"
PLATFORM_HEADER = "X-App-Platform"
OS_VERSION_HEADER = "X-OS-Version"

_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}(?:-[0-9A-Za-z.]{1,40})?$")
_PLATFORM = re.compile(r"^(darwin|linux|win32)-(arm64|x64|ia32|universal)$")
_OS_VERSION = re.compile(r"[^0-9A-Za-z .()_-]")

#: Node's platform names → the names releases are published under.
RELEASE_PLATFORM = {"darwin": "mac", "linux": "linux", "win32": "windows"}


@dataclass(frozen=True)
class ClientInfo:
    kind: str  # "desktop" | "web"
    version: str | None = None
    os: str | None = None  # darwin | linux | win32
    arch: str | None = None  # arm64 | x64 | …
    os_version: str | None = None

    @property
    def is_desktop(self) -> bool:
        return self.kind == "desktop"

    @property
    def release_platform(self) -> str | None:
        return RELEASE_PLATFORM.get(self.os or "")


WEB = ClientInfo(kind="web")


def read_client(request: Request) -> ClientInfo:
    if (request.headers.get(CLIENT_HEADER) or "").strip().lower() != "desktop":
        return WEB
    version = (request.headers.get(VERSION_HEADER) or "").strip()
    platform = (request.headers.get(PLATFORM_HEADER) or "").strip().lower()
    os_name = arch = None
    if match := _PLATFORM.match(platform):
        os_name, arch = match.group(1), match.group(2)
    os_version = _OS_VERSION.sub("", request.headers.get(OS_VERSION_HEADER) or "")[:60].strip()
    return ClientInfo(
        kind="desktop",
        version=version if _VERSION.match(version) else None,
        os=os_name,
        arch=arch,
        os_version=os_version or None,
    )


def get_client(request: Request) -> ClientInfo:
    return read_client(request)


Client = Annotated[ClientInfo, Depends(get_client)]


def version_key(version: str | None) -> tuple:
    """Sort key for ``major.minor.patch[-pre]``: a pre-release sorts before its release.

    Anything unreadable sorts below every real version, so a client that sends
    garbage is always told to update.
    """
    if not version or not _VERSION.match(version):
        return (-1,)
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    # (…, 1) for a final release beats (…, 0, "beta") for a pre-release.
    return (major, minor, patch, 1, "") if not pre else (major, minor, patch, 0, pre)


def is_valid_version(version: str) -> bool:
    return bool(_VERSION.match(version or ""))
