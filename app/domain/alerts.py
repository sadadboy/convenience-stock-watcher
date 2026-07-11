from dataclasses import dataclass
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


@dataclass
class AlertDecision:
    should_alert: bool
    next_status: WatchStatus
    next_check_at: datetime | None = None
    paused_until: datetime | None = None
    alert_count: int = 0


def decide_stock_found(
    *,
    current_alert_count: int,
    max_alert_count: int,
    check_interval_seconds: int,
    pause_after_alerts_seconds: int,
    now: datetime | None = None,
) -> AlertDecision:
    checked_at = now or datetime.now(timezone.utc)
    next_alert_count = current_alert_count + 1

    if next_alert_count >= max_alert_count:
        return AlertDecision(
            should_alert=True,
            next_status=WatchStatus.PAUSED,
            paused_until=checked_at + timedelta(seconds=pause_after_alerts_seconds),
            alert_count=next_alert_count,
        )

    return AlertDecision(
        should_alert=True,
        next_status=WatchStatus.ACTIVE,
        next_check_at=checked_at + timedelta(seconds=check_interval_seconds),
        alert_count=next_alert_count,
    )


def decide_stock_missing(
    *,
    check_interval_seconds: int,
    now: datetime | None = None,
) -> AlertDecision:
    checked_at = now or datetime.now(timezone.utc)

    return AlertDecision(
        should_alert=False,
        next_status=WatchStatus.ACTIVE,
        next_check_at=checked_at + timedelta(seconds=check_interval_seconds),
        alert_count=0,
    )
