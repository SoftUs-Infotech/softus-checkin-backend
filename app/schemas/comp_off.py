"""Comp-off API shapes."""

from pydantic import BaseModel, ConfigDict

from app.models.comp_off import CompOffStatus


class CompOffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    #: Stored on the credit when it is issued, so the row still names somebody
    #: after the account is renamed or removed.
    user_email: str | None = None
    #: Resolved when the organisation ledger is read; absent elsewhere.
    user_name: str | None = None
    earned_on: str
    reason: str
    expires_on: str | None = None
    #: Resolved against today, so an expired credit reads as expired.
    status: CompOffStatus
    used_on: str | None = None


class CompOffBalanceOut(BaseModel):
    user_id: str
    available: int
    used: int
    expired: int
    total: int
