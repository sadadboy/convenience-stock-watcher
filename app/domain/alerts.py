from datetime import datetime, timedelta, timezone
from enum import StrEnum


class WatchStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"
    DISABLED = "disabled"


class StockStatus(StrEnum):
    UNKNOWN = "unknown"
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"


def next_check_time(*, check_interval_seconds: int, now: datetime | None = None) -> datetime:
    """Watches never pause on stock: every successful check schedules the next one."""
    checked_at = now or datetime.now(timezone.utc)
    return checked_at + timedelta(seconds=check_interval_seconds)
