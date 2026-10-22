"""One decision, many records.

Shared by corrections and leave because the shape of "approve these twenty"
does not differ between them, and two copies would drift.
"""

from pydantic import BaseModel, Field


class BulkDecision(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class BulkResult(BaseModel):
    id: str
    success: bool
    error_code: str | None = None
    message: str | None = None


class BulkDecisionOut(BaseModel):
    approved: bool
    requested: int
    succeeded: int
    failed: int
    results: list[BulkResult]
