"""Shared Jinja2 environment for the HTML screens."""

from datetime import datetime, timedelta, timezone

from fastapi.templating import Jinja2Templates

# Timestamps are stored in UTC but the screens are read in Korea. KST has no
# DST, so a fixed +09:00 offset is exact -- and unlike ZoneInfo it needs no
# tzdata, which the slim base image does not ship.
KST = timezone(timedelta(hours=9))


def to_kst(value: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
    """Format a stored UTC timestamp in Korean local time."""
    if value is None:
        return ""
    if value.tzinfo is None:
        # Guard against a naive value from the driver: astimezone() would
        # otherwise read it as the container's local time and be silently wrong.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime(fmt)


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["kst"] = to_kst
