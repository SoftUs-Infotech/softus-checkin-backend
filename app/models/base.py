"""Shared document base for everything stored in MongoDB.

Models describe what lives in the database. They are deliberately separate from
the schemas in ``app/schemas`` which describe what crosses the API boundary — the
two are allowed to drift, and mixing them is how internal fields leak out.
"""

from datetime import date, datetime
from typing import Annotated, Any, TypeVar

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.core.datetime import utc_now


def _validate_object_id(value: Any) -> str:
    """Accept an ObjectId or its string form; always hand back a string."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return value
    raise ValueError(f"'{value}' is not a valid ObjectId.")


PyObjectId = Annotated[str, BeforeValidator(_validate_object_id)]


class MongoModel(BaseModel):
    """Base document: identity, timestamps, and a soft-delete flag."""

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )

    id: PyObjectId | None = Field(default=None, alias="_id")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    is_deleted: bool = False

    def to_mongo(self, *, exclude_none: bool = False) -> dict[str, Any]:
        """Serialise for insertion. ``_id`` is left to MongoDB when unset."""
        document = encode_for_mongo(
            self.model_dump(by_alias=True, exclude_none=exclude_none)
        )
        if document.get("_id") is None:
            document.pop("_id", None)
        else:
            document["_id"] = ObjectId(document["_id"])
        return document


def encode_for_mongo(value: Any) -> Any:
    """BSON has no ``date`` type — store plain dates as ``YYYY-MM-DD`` strings.

    Datetimes are left alone (BSON stores them natively) and are always UTC by
    the time they get here.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: encode_for_mongo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_for_mongo(item) for item in value]
    return value


ModelT = TypeVar("ModelT", bound=MongoModel)


def to_object_id(value: str | ObjectId) -> ObjectId:
    """Convert an id from the API layer into a query-ready ObjectId."""
    if isinstance(value, ObjectId):
        return value
    if not ObjectId.is_valid(value):
        raise ValueError(f"'{value}' is not a valid ObjectId.")
    return ObjectId(value)


def serialize(document: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn a raw Mongo document into something JSON-serialisable."""
    if document is None:
        return None
    result = dict(document)
    if isinstance(result.get("_id"), ObjectId):
        result["id"] = str(result.pop("_id"))
    for key, value in result.items():
        if isinstance(value, ObjectId):
            result[key] = str(value)
    return result


def touch(update: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``updated_at`` on a ``$set`` payload."""
    update = dict(update)
    update["updated_at"] = utc_now()
    return update
