"""Desktop app releases: upload, publish, and "is this app up to date?".

An administrator uploads the build files of a new version (they become a draft
release), then publishes it. From then on every desktop app on that platform
that is older is told to update before it may punch in — the app downloads the
file, checks its SHA-256 against the one recorded here, and replaces itself.
"""

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from bson import ObjectId
from fastapi import UploadFile
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.datetime import to_utc, utc_now
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.app_release import (
    CONTENT_TYPE,
    KINDS_FOR,
    UPDATE_KIND,
    AppRelease,
    ReleaseFile,
    ReleaseFileKind,
    ReleasePlatform,
    ReleaseStatus,
)
from app.models.audit_log import AuditAction, AuditEntity
from app.services import audit
from app.services.audit import Actor
from app.utils.client import ClientInfo, is_valid_version, version_key
from app.utils.storage import get_storage

logger = get_logger(__name__)

_CHUNK = 1024 * 1024
_ARCH_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x64": "x64",
    "x86_64": "x64",
    "amd64": "x64",
    "universal": "universal",
}
_FILENAME_ARCH = re.compile(
    r"(?:^|[-_. ])(arm64|aarch64|x64|x86_64|amd64|universal)(?=[-_. ])", re.I
)
_FILENAME_VERSIONS = re.compile(r"\d+\.\d+\.\d+")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSIONS = {
    ".zip": ReleaseFileKind.ZIP,
    ".dmg": ReleaseFileKind.DMG,
    ".appimage": ReleaseFileKind.APPIMAGE,
}


def _releases():
    return get_collection(Collections.APP_RELEASES)


def _object_id(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError("That release does not exist.", code="RELEASE_NOT_FOUND")
    return ObjectId(value)


async def _get(release_id: str) -> dict[str, Any]:
    document = await _releases().find_one({"_id": _object_id(release_id)})
    if document is None:
        raise NotFoundError("That release does not exist.", code="RELEASE_NOT_FOUND")
    return document


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _file_out(file: dict[str, Any]) -> dict[str, Any]:
    storage = get_storage(file.get("storage_provider"))
    return {
        "arch": file["arch"],
        "kind": file["kind"],
        "filename": file["filename"],
        "size_bytes": file["size_bytes"],
        "sha256": file["sha256"],
        "uploaded_at": to_utc(file["uploaded_at"]),
        "url": storage.url_for(
            file["storage_key"],
            CONTENT_TYPE[ReleaseFileKind(file["kind"])],
            expires_minutes=settings.RELEASE_URL_EXPIRE_MINUTES,
            filename=file["filename"],
        ),
    }


def _updatable(document: dict[str, Any], arch: str | None = None) -> bool:
    """Has the file the app updates itself from (for ``arch``, if given)."""
    kind = UPDATE_KIND[ReleasePlatform(document["platform"])]
    return any(
        f["kind"] == kind and (arch is None or f["arch"] in (arch, "universal"))
        for f in document.get("files", [])
    )


def to_out(document: dict[str, Any], *, current: bool = False) -> dict[str, Any]:
    files = sorted(document.get("files", []), key=lambda f: (f["kind"], f["arch"]))
    return {
        "id": str(document["_id"]),
        "version": document["version"],
        "platform": document["platform"],
        "notes": document.get("notes", ""),
        "status": document["status"],
        "files": [_file_out(f) for f in files],
        "created_at": to_utc(document["created_at"]),
        "published_at": to_utc(document["published_at"]) if document.get("published_at") else None,
        "updatable": _updatable(document),
        "current": current,
    }


async def current_release(platform: str, arch: str | None = None) -> dict[str, Any] | None:
    """The newest published release of a platform (that can update ``arch``, if given)."""
    published = [
        document
        async for document in _releases().find(
            {"platform": platform, "status": ReleaseStatus.PUBLISHED.value}
        )
        if arch is None or _updatable(document, arch)
    ]
    if not published:
        return None
    return max(published, key=lambda document: version_key(document["version"]))


async def list_all() -> list[dict[str, Any]]:
    current_ids = set()
    for platform in ReleasePlatform:
        current = await current_release(platform.value)
        if current:
            current_ids.add(current["_id"])
    documents = [document async for document in _releases().find({})]
    documents.sort(key=lambda d: (d["platform"], version_key(d["version"])), reverse=True)
    documents.sort(key=lambda d: d["platform"])
    return [to_out(d, current=d["_id"] in current_ids) for d in documents]


async def published_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for platform in ReleasePlatform:
        current = await current_release(platform.value)
        out[platform.value] = current["version"] if current else None
    return out


async def latest_for(platform: str, arch: str | None) -> dict[str, Any] | None:
    """What an app on ``platform``/``arch`` should be running, with its download links.

    Only the files that machine can use: its own processor's, or universal ones.
    """
    try:
        ReleasePlatform(platform)
    except ValueError:
        raise ValidationError("Unknown platform.", code="UNKNOWN_PLATFORM") from None
    current = await current_release(platform, arch)
    if current is None:
        return None
    out = to_out(current, current=True)
    if arch:
        out["files"] = [f for f in out["files"] if f["arch"] in (arch, "universal")]
    return out


async def require_current(client: ClientInfo) -> None:
    """Refuse a punch-in from a desktop app older than the published version.

    A browser is never asked to update, and neither is a machine the published
    release has no file for — telling somebody to install what does not exist
    would only lock them out.
    """
    if not client.is_desktop or client.release_platform not in {p.value for p in ReleasePlatform}:
        return
    current = await current_release(client.release_platform, client.arch)
    if current is None:
        return
    if version_key(client.version) < version_key(current["version"]):
        raise ConflictError(
            "A new version of the app is ready. Update it to punch in.",
            code="APP_UPDATE_REQUIRED",
            details={"version": current["version"]},
        )


# --------------------------------------------------------------------------- #
# uploading
# --------------------------------------------------------------------------- #
def _kind_of(filename: str) -> ReleaseFileKind:
    suffix = Path(filename).suffix.lower()
    if suffix not in _EXTENSIONS:
        raise ValidationError(
            "Upload a .dmg or .zip (Mac) or an .AppImage (Linux).", code="UNSUPPORTED_RELEASE_FILE"
        )
    return _EXTENSIONS[suffix]


def _arch_of(filename: str, given: str | None) -> str:
    if given:
        arch = _ARCH_ALIASES.get(given.strip().lower())
        if arch is None:
            raise ValidationError("Processor must be arm64, x64 or universal.", code="UNKNOWN_ARCH")
        return arch
    match = _FILENAME_ARCH.search(Path(filename).stem + ".")
    if match is None:
        raise ValidationError(
            "Could not tell which processor this file is for. Choose arm64, x64 or universal.",
            code="ARCH_REQUIRED",
        )
    return _ARCH_ALIASES[match.group(1).lower()]


def _looks_like(kind: ReleaseFileKind, head: bytes, tail: bytes) -> bool:
    if kind is ReleaseFileKind.ZIP:
        return head.startswith(b"PK\x03\x04")
    if kind is ReleaseFileKind.APPIMAGE:
        return head.startswith(b"\x7fELF")
    # A disk image ends with a 512-byte "koly" trailer.
    return tail[-512:].startswith(b"koly")


async def upload(
    *,
    version: str,
    platform: str,
    notes: str | None,
    arch: str | None,
    file: UploadFile,
    actor: Actor,
) -> dict[str, Any]:
    version = (version or "").strip()
    if not is_valid_version(version):
        raise ValidationError(
            "Version must look like 1.2.3 (optionally 1.2.3-beta.1).", code="INVALID_VERSION"
        )
    try:
        platform_enum = ReleasePlatform(platform)
    except ValueError:
        raise ValidationError("Platform must be mac or linux.", code="UNKNOWN_PLATFORM") from None

    original = Path(file.filename or "").name
    kind = _kind_of(original)
    if kind not in KINDS_FOR[platform_enum]:
        raise ValidationError(
            f"A {platform_enum.value} release takes "
            + " or ".join(sorted(f".{k.value}" for k in KINDS_FOR[platform_enum]))
            + " files.",
            code="WRONG_FILE_FOR_PLATFORM",
        )
    named_versions = _FILENAME_VERSIONS.findall(original)
    if named_versions and version.split("-")[0] not in named_versions:
        raise ValidationError(
            f"This file is version {named_versions[0]}, not {version}.", code="VERSION_MISMATCH"
        )
    file_arch = _arch_of(original, arch)
    filename = _UNSAFE_NAME.sub("-", original).strip("-") or f"softus-checkin-{version}"

    existing = await _releases().find_one({"platform": platform_enum.value, "version": version})
    if existing and existing["status"] != ReleaseStatus.DRAFT.value:
        raise ConflictError(
            f"Version {version} has already been published. Build and upload a new version number.",
            code="RELEASE_NOT_DRAFT",
        )

    # Streamed to disk while hashing: a build is far too large to hold in memory.
    hasher = hashlib.sha256()
    size = 0
    head = b""
    tail = b""
    handle, temp_name = tempfile.mkstemp(prefix="release-", suffix=Path(filename).suffix)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > settings.RELEASE_MAX_BYTES:
                    raise ValidationError(
                        "That file is larger than an app build should be.",
                        code="RELEASE_TOO_LARGE",
                    )
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                tail = (tail + chunk)[-1024:]
                hasher.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ValidationError("The file arrived empty.", code="EMPTY_RELEASE_FILE")
        if not _looks_like(kind, head, tail):
            raise ValidationError(
                f"That is not a real .{kind.value} file.", code="NOT_A_RELEASE_FILE"
            )

        storage = get_storage()
        key = f"releases/{platform_enum.value}/{version}/{filename}"
        await storage.put_file(key, temp_path, CONTENT_TYPE[kind])
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink()

    entry = ReleaseFile(
        arch=file_arch,
        kind=kind,
        filename=filename,
        storage_key=key,
        storage_provider=storage.name,
        size_bytes=size,
        sha256=hasher.hexdigest(),
    ).model_dump()

    if existing is None:
        document = AppRelease(
            version=version,
            platform=platform_enum,
            notes=(notes or "").strip(),
            files=[entry],
            created_by=actor.id,
        ).to_mongo()
        try:
            result = await _releases().insert_one(document)
            document["_id"] = result.inserted_id
        except DuplicateKeyError:
            # Two files of a new version uploaded at once: add to the other one.
            existing = await _releases().find_one(
                {"platform": platform_enum.value, "version": version}
            )
    if existing is not None:
        # The same processor and kind uploaded again replaces the earlier file.
        replaced = [
            f
            for f in existing.get("files", [])
            if f["arch"] == file_arch and f["kind"] == kind.value
        ]
        kept = [f for f in existing.get("files", []) if f not in replaced]
        update: dict[str, Any] = {"files": [*kept, entry], "updated_at": utc_now()}
        if notes is not None and notes.strip():
            update["notes"] = notes.strip()
        await _releases().update_one({"_id": existing["_id"]}, {"$set": update})
        stale = [f["storage_key"] for f in replaced if f["storage_key"] != key]
        if stale:
            await get_storage().delete_many(stale)
        document = await _releases().find_one({"_id": existing["_id"]})

    await audit.record(
        actor,
        AuditAction.UPDATE if existing else AuditAction.CREATE,
        AuditEntity.APP_RELEASE,
        entity_id=str(document["_id"]),
        entity_label=f"{platform_enum.value} {version}: {filename}",
        after={"file": filename, "arch": file_arch, "size_bytes": size},
    )
    return to_out(document)


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
async def publish(release_id: str, actor: Actor) -> dict[str, Any]:
    document = await _get(release_id)
    if document["status"] == ReleaseStatus.PUBLISHED.value:
        return to_out(document, current=True)
    if not _updatable(document):
        kind = UPDATE_KIND[ReleasePlatform(document["platform"])]
        raise ValidationError(
            f"Upload the .{kind.value} file first: it is what the app updates itself from.",
            code="RELEASE_INCOMPLETE",
        )
    current = await current_release(document["platform"])
    if current and version_key(document["version"]) <= version_key(current["version"]):
        raise ConflictError(
            f"Version {current['version']} is already published. Publish a higher version number.",
            code="RELEASE_NOT_NEWER",
        )
    now = utc_now()
    await _releases().update_one(
        {"_id": document["_id"]},
        {
            "$set": {
                "status": ReleaseStatus.PUBLISHED.value,
                "published_at": now,
                "published_by": actor.id,
                "updated_at": now,
            }
        },
    )
    await audit.record(
        actor,
        AuditAction.PUBLISH,
        AuditEntity.APP_RELEASE,
        entity_id=release_id,
        entity_label=f"{document['platform']} {document['version']}",
    )
    logger.info("Desktop app %s %s published", document["platform"], document["version"])
    return to_out(await _get(release_id), current=True)


async def withdraw(release_id: str, actor: Actor) -> dict[str, Any]:
    document = await _get(release_id)
    if document["status"] != ReleaseStatus.PUBLISHED.value:
        raise ConflictError(
            "Only a published release can be withdrawn.", code="RELEASE_NOT_PUBLISHED"
        )
    await _releases().update_one(
        {"_id": document["_id"]},
        {"$set": {"status": ReleaseStatus.WITHDRAWN.value, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.UPDATE,
        AuditEntity.APP_RELEASE,
        entity_id=release_id,
        entity_label=f"{document['platform']} {document['version']} withdrawn",
    )
    return to_out(await _get(release_id))


async def delete(release_id: str, actor: Actor) -> None:
    document = await _get(release_id)
    if document["status"] == ReleaseStatus.PUBLISHED.value:
        raise ConflictError(
            "Withdraw a published release before deleting it.", code="RELEASE_PUBLISHED"
        )
    by_provider: dict[str, list[str]] = {}
    for f in document.get("files", []):
        by_provider.setdefault(f["storage_provider"], []).append(f["storage_key"])
    for provider, keys in by_provider.items():
        await get_storage(provider).delete_many(keys)
    await _releases().delete_one({"_id": document["_id"]})
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.APP_RELEASE,
        entity_id=release_id,
        entity_label=f"{document['platform']} {document['version']}",
    )
