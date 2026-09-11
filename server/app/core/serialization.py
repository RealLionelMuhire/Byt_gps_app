"""Shared datetime serialization helpers.

All datetimes are stored as naive UTC in the database. Pydantic/FastAPI
serializes naive datetimes without a timezone suffix, which makes browsers
parse them as *local* time — so an expiry stored as 12:00 UTC displays as
12:00 local (2h early in UTC+2) and the frontend's "expired/overdue" verdict
drifts from the backend's. Annotating response fields with ``UtcDateTime``
forces serialization with an explicit UTC marker ("Z") so clients parse the
same instant the server computed.
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import PlainSerializer


def _to_utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


# JSON-mode serializer only: python-mode (ORM round-trips) stays untouched.
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(_to_utc_iso, return_type=str, when_used="json"),
]