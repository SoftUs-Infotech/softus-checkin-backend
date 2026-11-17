"""Serving files kept in the local storage folder.

Only used with ``STORAGE_PROVIDER=local`` (S3 files are reached through
presigned URLs instead). There is no bearer token here — an ``<img>`` tag cannot
send one — so the link itself is the permission: it is signed with
``SECRET_KEY`` and expires, see ``app/utils/storage.py``.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.core.exceptions import NotFoundError
from app.utils.storage import LocalStorage, get_storage, verify_local_link

router = APIRouter(tags=["files"])

_SERVABLE = {"image/jpeg", "image/png", "image/webp"}


@router.get("/files", summary="A locally stored file, through a signed link")
async def read_file(
    key: Annotated[str, Query(min_length=1, max_length=300)],
    exp: int,
    sig: Annotated[str, Query(min_length=1, max_length=128)],
    type: Annotated[str, Query(max_length=40)] = "image/jpeg",
) -> Response:
    verify_local_link(key, exp, sig)
    storage = cast(LocalStorage, get_storage("local"))
    try:
        data = await storage.read(key)
    except ValueError:
        data = None
    if data is None:
        raise NotFoundError("This file no longer exists.", code="FILE_NOT_FOUND")
    return Response(
        content=data,
        media_type=type if type in _SERVABLE else "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
