"""Audit log API shapes. Read-only by design — there is no create schema."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.audit_log import AuditAction, AuditEntity


class AuditQuery(BaseModel):
    actor_id: str | None = None
    entity: AuditEntity | None = None
    entity_id: str | None = None
    action: AuditAction | None = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str | None = None
    actor_email: str | None = None
    action: AuditAction
    entity: AuditEntity
    entity_id: str | None = None
    entity_label: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    ip: str | None = None
    at: datetime

    @classmethod
    def from_document(cls, document: dict) -> "AuditLogOut":
        return cls.model_validate(document)
