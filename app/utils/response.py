"""Helpers that put every route's output into the same envelope."""

from typing import Any

from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def success(
    data: Any = None,
    message: str = "OK",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return value for a route. FastAPI serialises it."""
    return {"success": True, "message": message, "data": data, "meta": meta}


def created(data: Any = None, message: str = "Created.") -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=jsonable_encoder(success(data, message)),
    )


def error(
    message: str,
    code: str = "ERROR",
    status_code: int = status.HTTP_400_BAD_REQUEST,
    details: Any = None,
) -> JSONResponse:
    """Rarely needed — prefer raising an ``AppError`` and letting a handler render it."""
    body: dict[str, Any] = {
        "success": False,
        "message": message,
        "data": None,
        "error": {"code": code},
        "meta": None,
    }
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))
