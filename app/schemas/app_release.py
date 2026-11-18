"""Desktop app release shapes."""

from datetime import datetime

from pydantic import BaseModel

from app.models.app_release import ReleaseFileKind, ReleasePlatform, ReleaseStatus


class ReleaseFileOut(BaseModel):
    arch: str
    kind: ReleaseFileKind
    filename: str
    size_bytes: int
    sha256: str
    uploaded_at: datetime
    #: Short-lived download link. Absolute for S3; for local storage a path
    #: under the API prefix (``/files?…``) that the client joins onto its API address.
    url: str


class AppReleaseOut(BaseModel):
    id: str
    version: str
    platform: ReleasePlatform
    notes: str = ""
    status: ReleaseStatus
    files: list[ReleaseFileOut]
    created_at: datetime
    published_at: datetime | None = None
    #: Has the file the app updates itself from — required before publishing.
    updatable: bool = False
    #: The newest published release of its platform: what every app must be on.
    current: bool = False


class PublishedVersions(BaseModel):
    """The version each platform's app has to be on, or ``None`` when none is published."""

    mac: str | None = None
    linux: str | None = None
    windows: str | None = None
