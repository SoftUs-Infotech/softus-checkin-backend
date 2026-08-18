"""Holiday API shapes."""

from datetime import date as date_type

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.holiday import HolidayType

# Aliased because these models have a field called ``date``. In ``x: ann = value``
# CPython binds ``x`` before it evaluates ``ann``, so ``date: date | None = None``
# would resolve ``date`` to the freshly bound ``None``.


class HolidayCreate(BaseModel):
    date: date_type
    name: str = Field(min_length=2, max_length=100)
    type: HolidayType = HolidayType.PUBLIC
    description: str | None = Field(default=None, max_length=500)
    applicable_departments: list[str] = Field(default_factory=list)


class HolidayBulkCreate(BaseModel):
    """Load a whole year in one call, which is how holidays actually arrive."""

    holidays: list[HolidayCreate] = Field(min_length=1, max_length=200)
    replace_existing: bool = False


class HolidayUpdate(BaseModel):
    date: date_type | None = None
    name: str | None = Field(default=None, min_length=2, max_length=100)
    type: HolidayType | None = None
    description: str | None = Field(default=None, max_length=500)
    applicable_departments: list[str] | None = None
    is_active: bool | None = None


class HolidayQuery(BaseModel):
    year: int | None = Field(default=None, ge=2000, le=2100)
    from_date: date_type | None = None
    to_date: date_type | None = None
    include_inactive: bool = False

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "HolidayQuery":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("'from_date' must not be after 'to_date'.")
        return self


class HolidayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    date: str
    name: str
    type: HolidayType
    description: str | None = None
    applicable_departments: list[str] = Field(default_factory=list)
    year: int
    is_active: bool = True
    weekday: str | None = None

    @classmethod
    def from_document(cls, document: dict) -> "HolidayOut":
        from app.schemas.shift import WEEKDAY_NAMES

        data = dict(document)
        try:
            data["weekday"] = WEEKDAY_NAMES[date_type.fromisoformat(data["date"]).weekday()]
        except (ValueError, KeyError):
            data["weekday"] = None
        return cls.model_validate(data)
