"""Envelope and pagination shapes shared by every endpoint."""

from math import ceil
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ResponseModel(BaseModel, Generic[T]):
    """The single response envelope every endpoint returns."""

    success: bool = True
    message: str = "OK"
    data: T | None = None
    meta: dict[str, Any] | None = None


class ErrorDetail(BaseModel):
    code: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    data: None = None
    error: ErrorDetail
    meta: dict[str, Any] | None = None


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class PaginatedData(BaseModel, Generic[T]):
    items: list[T]
    pagination: PageMeta

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def build(cls, items: list[T], *, page: int, page_size: int, total: int) -> "PaginatedData[T]":
        total_pages = ceil(total / page_size) if page_size else 0
        return cls(
            items=items,
            pagination=PageMeta(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=total_pages,
                has_next=page < total_pages,
                has_previous=page > 1,
            ),
        )


class MessageData(BaseModel):
    """For endpoints whose only meaningful output is confirmation."""

    detail: str
