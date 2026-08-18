"""Public and company holidays.

``date`` is stored as a ``YYYY-MM-DD`` string in the organisation's timezone —
the same key the attendance collection uses, so a day lookup is a direct match.
"""

from enum import StrEnum

from pydantic import Field

from app.models.base import MongoModel


class HolidayType(StrEnum):
    PUBLIC = "PUBLIC"
    COMPANY = "COMPANY"
    OPTIONAL = "OPTIONAL"  # restricted holiday — employee may choose to take it


class Holiday(MongoModel):
    date: str
    name: str
    type: HolidayType = HolidayType.PUBLIC
    description: str | None = Field(default=None, max_length=500)

    # Empty means every department.
    applicable_departments: list[str] = Field(default_factory=list)

    year: int
    is_active: bool = True

    def applies_to(self, department: str | None) -> bool:
        if not self.applicable_departments:
            return True
        return department in self.applicable_departments
