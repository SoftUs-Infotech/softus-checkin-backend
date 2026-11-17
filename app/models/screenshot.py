"""A screenshot of a work-from-home employee's screen.

The image itself lives in storage (a local folder or S3); this document is the
index that says whose it is, when it was taken and where to find it. Deleting a
screenshot removes both — the point of deleting is to free the space.
"""

from datetime import datetime

from app.models.base import MongoModel


class Screenshot(MongoModel):
    user_id: str
    #: The organisation-local day it was taken on, ``YYYY-MM-DD``.
    date: str
    #: Server time on arrival, not the browser's claim.
    captured_at: datetime
    storage_key: str
    #: Which provider holds the file, so a later switch from the local folder
    #: to S3 does not strand older files.
    storage_provider: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
