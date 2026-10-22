"""Correction request API shapes."""

from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.correction import ApprovalStatus, CorrectionType


class CorrectionCreate(BaseModel):
    date: date_type
    type: CorrectionType
    requested_punch_in: datetime | None = None
    requested_punch_out: datetime | None = None
    reason: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def _times_match_the_request_type(self) -> "CorrectionCreate":
        if self.type is CorrectionType.MISSED_PUNCH_IN and self.requested_punch_in is None:
            raise ValueError("Give the punch-in time you are asking for.")
        if self.type is CorrectionType.MISSED_PUNCH_OUT and self.requested_punch_out is None:
            raise ValueError("Give the punch-out time you are asking for.")
        if self.type in (CorrectionType.WRONG_TIME, CorrectionType.FULL_DAY) and not (
            self.requested_punch_in and self.requested_punch_out
        ):
            raise ValueError("Give both the punch-in and punch-out times you are asking for.")
        if (
            self.requested_punch_in
            and self.requested_punch_out
            and self.requested_punch_out <= self.requested_punch_in
        ):
            raise ValueError("Punch-out must be after punch-in.")
        return self


class CorrectionDecision(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class CorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    user_email: str | None = None
    user_name: str | None = None
    date: str
    type: CorrectionType
    requested_punch_in: datetime | None = None
    requested_punch_out: datetime | None = None
    reason: str
    status: ApprovalStatus
    approver_email: str | None = None
    approver_role: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, document: dict) -> "CorrectionOut":
        return cls.model_validate(document)
