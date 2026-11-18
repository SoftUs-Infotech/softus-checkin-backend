"""Serving files kept in the local storage folder.

Only used with ``STORAGE_PROVIDER=local`` (S3 files are reached through
presigned URLs instead). There is no bearer token here — an ``<img>`` tag cannot
send one — so the link itself is the permission: it is signed with
``SECRET_KEY`` and expires, see ``app/utils/storage.py``.
"""

import re
from typing import Annotated, cast

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app.core.exceptions import NotFoundError
from app.utils.storage import LocalStorage, get_storage, verify_local_link

router = APIRouter(tags=["files"])

#: Images are shown in the page; app builds are downloaded.
_SERVABLE = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/zip",
    "application/x-apple-diskimage",
    "application/octet-stream",
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@router.get("/files", summary="A locally stored file, through a signed link")
async def read_file(
    key: Annotated[str, Query(min_length=1, max_length=300)],
    exp: int,
    sig: Annotated[str, Query(min_length=1, max_length=128)],
    type: Annotated[str, Query(max_length=40)] = "image/jpeg",
    name: Annotated[str | None, Query(max_length=120)] = None,
) -> FileResponse:
    verify_local_link(key, exp, sig)
    storage = cast(LocalStorage, get_storage("local"))
    try:
        path = storage.path_for(key)
    except ValueError:
        path = None
    if path is None or not path.is_file():
        raise NotFoundError("This file no longer exists.", code="FILE_NOT_FOUND")
    # Streamed from disk: an app build is far too large to read into memory.
    return FileResponse(
        path,
        media_type=type if type in _SERVABLE else "application/octet-stream",
        filename=_SAFE_NAME.sub("_", name) if name else None,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
