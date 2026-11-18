"""A version of the desktop app, uploaded and published from the admin panel.

One release is one version for one platform (``mac``, ``linux`` or ``windows``).
It holds the files of that version — for the Mac a ``.zip`` (what the app
updates itself from) and a ``.dmg`` (what a person installs the first time), per
processor; for Linux an ``.AppImage`` and for Windows an installer ``.exe``,
each of which is both.

A release is a draft until an administrator publishes it. The newest published
release of a platform is the version every desktop app on that platform has to
be on before it may punch in.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.datetime import utc_now
from app.models.base import MongoModel


class ReleasePlatform(StrEnum):
    MAC = "mac"
    LINUX = "linux"
    WINDOWS = "windows"


class ReleaseStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    #: Taken back: no longer offered, so the published release before it is
    #: the current one again.
    WITHDRAWN = "WITHDRAWN"


class ReleaseFileKind(StrEnum):
    ZIP = "zip"  # Mac: the app updates itself from this
    DMG = "dmg"  # Mac: first install
    APPIMAGE = "appimage"  # Linux: install and update
    EXE = "exe"  # Windows: the installer, for the first install and for updates


#: Which kind of file the app downloads to update itself, per platform.
UPDATE_KIND = {
    ReleasePlatform.MAC: ReleaseFileKind.ZIP,
    ReleasePlatform.LINUX: ReleaseFileKind.APPIMAGE,
    ReleasePlatform.WINDOWS: ReleaseFileKind.EXE,
}
#: Which kinds a platform takes at all.
KINDS_FOR = {
    ReleasePlatform.MAC: {ReleaseFileKind.ZIP, ReleaseFileKind.DMG},
    ReleasePlatform.LINUX: {ReleaseFileKind.APPIMAGE},
    ReleasePlatform.WINDOWS: {ReleaseFileKind.EXE},
}
CONTENT_TYPE = {
    ReleaseFileKind.ZIP: "application/zip",
    ReleaseFileKind.DMG: "application/x-apple-diskimage",
    ReleaseFileKind.APPIMAGE: "application/octet-stream",
    ReleaseFileKind.EXE: "application/vnd.microsoft.portable-executable",
}


class ReleaseFile(BaseModel):
    #: ``arm64`` (Apple silicon), ``x64`` (Intel / PC), or ``universal``.
    arch: str
    kind: ReleaseFileKind
    filename: str
    storage_key: str
    storage_provider: str
    size_bytes: int
    #: Checked by the app before it replaces itself with the download.
    sha256: str
    uploaded_at: datetime = Field(default_factory=utc_now)


class AppRelease(MongoModel):
    version: str
    platform: ReleasePlatform
    notes: str = ""
    status: ReleaseStatus = ReleaseStatus.DRAFT
    files: list[ReleaseFile] = Field(default_factory=list)
    created_by: str | None = None
    published_at: datetime | None = None
    published_by: str | None = None
