"""Desktop app releases.

Administrators upload a version's build files, then publish it. Any signed-in
person can ask which version their app should be on (the desktop app does, at
punch-in) and get download links for it.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.schemas.app_release import AppReleaseOut, PublishedVersions
from app.services import app_release as release_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/app-releases", tags=["desktop app"])


@router.get("/latest", summary="The version this app should be on, with download links")
async def latest_release(
    _: CurrentUser,
    platform: Annotated[str, Query(pattern="^(mac|linux|windows)$")],
    arch: Annotated[str | None, Query(pattern="^(arm64|x64|universal)$")] = None,
) -> dict:
    release = await release_service.latest_for(platform, arch)
    return success(AppReleaseOut(**release) if release else None)


@router.get("/published", summary="The published version of each platform")
async def published_versions(_: CurrentUser) -> dict:
    return success(PublishedVersions(**await release_service.published_versions()))


@router.get("", summary="Every release, drafts included")
async def list_releases(_: CurrentAdmin) -> dict:
    return success([AppReleaseOut(**r) for r in await release_service.list_all()])


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    summary="Upload one build file (creates the draft release on the first file)",
)
async def upload_release_file(
    _: CurrentAdmin,
    actor: CurrentActor,
    version: Annotated[str, Form(min_length=5, max_length=40)],
    platform: Annotated[str, Form(pattern="^(mac|linux|windows)$")],
    file: Annotated[
        UploadFile, File(description=".dmg / .zip (Mac), .AppImage (Linux) or .exe (Windows)")
    ],
    arch: Annotated[str | None, Form(max_length=20)] = None,
    notes: Annotated[str | None, Form(max_length=2000)] = None,
) -> dict:
    release = await release_service.upload(
        version=version, platform=platform, notes=notes, arch=arch or None, file=file, actor=actor
    )
    return success(AppReleaseOut(**release), message="File uploaded.")


@router.post("/{release_id}/publish", summary="Publish: every older app must update")
async def publish_release(release_id: str, _: CurrentAdmin, actor: CurrentActor) -> dict:
    release = await release_service.publish(release_id, actor)
    return success(AppReleaseOut(**release), message=f"Version {release['version']} published.")


@router.post("/{release_id}/withdraw", summary="Stop offering a published release")
async def withdraw_release(release_id: str, _: CurrentAdmin, actor: CurrentActor) -> dict:
    release = await release_service.withdraw(release_id, actor)
    return success(AppReleaseOut(**release), message=f"Version {release['version']} withdrawn.")


@router.delete("/{release_id}", summary="Delete a draft or withdrawn release and its files")
async def delete_release(release_id: str, _: CurrentAdmin, actor: CurrentActor) -> dict:
    await release_service.delete(release_id, actor)
    return success(None, message="Release deleted.")
