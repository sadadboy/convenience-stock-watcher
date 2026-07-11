"""Restock watcher: poll due watches, record events, raise restock alerts.

Alerts are delivered to the app log and the ``stock_watch_events`` table (shown
on the /watches screen). Real push channels (Discord/Telegram) can be added
later without changing this logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.domain.alerts import (
    StockStatus,
    WatchStatus,
    decide_stock_found,
    decide_stock_missing,
)
from app.models.restock import StockWatch, StockWatchEvent
from app.repositories.notifications import get_discord_webhook
from app.services.adapters.emart24 import check_emart24_stock
from app.services.adapters.gs25 import check_gs25_stock
from app.services.adapters.seven_eleven import check_seveneleven_stock
from app.services.notifications import send_discord_message
from app.services.store_adapter import StockLookup, StoreBrand

logger = logging.getLogger(__name__)


def _push_discord(db: Session, source, summary: str, result: StockLookup) -> None:
    """Send a restock alert to Discord if a webhook is configured. Never raises."""
    webhook = get_discord_webhook(db)
    if not webhook:
        return
    title = f"🔔 입고! [{source.brand}] {source.external_product_name}"
    message = (
        f"상품코드 {source.external_product_code}\n"
        f"재고 있는 매장 {result.in_stock_count}곳 · {summary}\n"
        f"조회 범위: {result.context}"
    )
    ok, error = send_discord_message(webhook, title, message)
    if not ok:
        logger.warning("Discord push failed for watch source %s: %s", source.id, error)


async def _lookup_stock(watch: StockWatch) -> StockLookup | None:
    """Dispatch to the right brand adapter using the watch's location. None if unsupported."""
    source = watch.source
    code = source.external_product_code
    keyword = watch.location_query
    lat, lon, radius = watch.latitude, watch.longitude, watch.radius_meters

    if source.brand == StoreBrand.SEVEN_ELEVEN.value:
        return await check_seveneleven_stock(
            code, store_keyword=keyword, latitude=lat, longitude=lon, radius_meters=radius
        )
    if source.brand == StoreBrand.EMART24.value:
        return await check_emart24_stock(
            code, store_keyword=keyword, latitude=lat, longitude=lon, radius_meters=radius
        )
    if source.brand == StoreBrand.GS25.value:
        if lat is not None and lon is not None:
            return await check_gs25_stock(code, latitude=lat, longitude=lon, radius_meters=radius)
        return await check_gs25_stock(code)
    return None


def _summarize(result: StockLookup, limit: int = 5) -> str:
    in_stock = [s for s in result.stores if s.in_stock]
    parts = [f"{s.store_name or s.store_code} {s.quantity}개" for s in in_stock[:limit]]
    extra = len(in_stock) - len(parts)
    if extra > 0:
        parts.append(f"외 {extra}곳")
    return ", ".join(parts)


def check_watch(db: Session, watch: StockWatch, *, now: datetime | None = None) -> StockWatchEvent:
    """Run one stock check for a watch, update it, and record an event."""
    now = now or datetime.now(timezone.utc)
    source = watch.source
    result = asyncio.run(_lookup_stock(watch))

    watch.last_checked_at = now
    watch.next_retry_at = None

    # Unsupported brand or failed lookup -> failure path.
    if result is None or not result.ok:
        message = (
            f"{source.brand} 재고 조회를 지원하지 않습니다."
            if result is None
            else (result.error or "재고 조회 실패")
        )
        watch.failure_count += 1
        watch.last_error = message
        watch.status = WatchStatus.FAILED.value
        watch.next_check_at = None
        watch.next_retry_at = now + _seconds(watch.failure_retry_seconds)
        event = _add_event(db, watch, kind="error", status="failed", quantity=None, message=message)
        logger.warning("watch %s failed: %s", watch.id, message)
        db.commit()
        return event

    watch.last_error = None
    watch.failure_count = 0
    in_stock = result.in_stock_count > 0
    total_qty = result.total_quantity

    if in_stock:
        summary = _summarize(result)
        decision = decide_stock_found(
            current_alert_count=watch.alert_count,
            max_alert_count=watch.max_alert_count,
            check_interval_seconds=watch.check_interval_seconds,
            pause_after_alerts_seconds=watch.pause_after_alerts_seconds,
            now=now,
        )
        watch.last_stock_status = StockStatus.IN_STOCK.value
        watch.last_stock_quantity = total_qty
        watch.last_in_stock_summary = summary
        watch.alert_count = decision.alert_count
        watch.status = decision.next_status.value
        watch.next_check_at = decision.next_check_at
        watch.paused_until = decision.paused_until
        watch.last_alerted_at = now

        message = f"입고 감지! {result.in_stock_count}개 매장 · {summary} ({result.context})"
        event = _add_event(
            db, watch, kind="restock", status="in_stock", quantity=total_qty,
            message=message, is_alert=True,
        )
        logger.warning(
            "🔔 RESTOCK alert: watch %s (%s %s) -> %s",
            watch.id, source.brand, source.external_product_code, summary,
        )
        _push_discord(db, source, summary, result)
    else:
        decision = decide_stock_missing(
            check_interval_seconds=watch.check_interval_seconds,
            now=now,
        )
        watch.last_stock_status = StockStatus.OUT_OF_STOCK.value
        watch.last_stock_quantity = 0
        watch.last_in_stock_summary = None
        watch.alert_count = decision.alert_count
        watch.status = decision.next_status.value
        watch.next_check_at = decision.next_check_at
        watch.paused_until = None

        message = f"재고 없음 (조회 {len(result.stores)}개 매장, {result.context})"
        event = _add_event(db, watch, kind="check", status="out_of_stock", quantity=0, message=message)

    db.commit()
    return event


def run_due_watches(db: Session, *, now: datetime | None = None) -> int:
    """Check due watches, capped per tick to spread load. Returns the number checked.

    When more watches are due than ``watcher_max_checks_per_tick``, only the
    most-overdue ones run this tick; the rest are picked up on later ticks, which
    naturally staggers their next check times and avoids API bursts.
    """
    now = now or datetime.now(timezone.utc)
    watches = db.scalars(
        select(StockWatch)
        .options(selectinload(StockWatch.source))
        .where(StockWatch.status != WatchStatus.DISABLED.value)
    ).all()

    due = [w for w in watches if _is_due(w, now)]
    # Oldest-due first (None sorts first), so nothing starves.
    due.sort(key=lambda w: w.next_check_at or datetime.min.replace(tzinfo=timezone.utc))
    cap = settings.watcher_max_checks_per_tick
    if cap and cap > 0:
        due = due[:cap]

    checked = 0
    for watch in due:
        if watch.status == WatchStatus.PAUSED.value:
            # Resume a paused watch whose pause window elapsed.
            watch.status = WatchStatus.ACTIVE.value
            watch.paused_until = None
            watch.alert_count = 0
        try:
            check_watch(db, watch, now=now)
            checked += 1
        except Exception:  # noqa: BLE001 - never let one watch break the loop
            db.rollback()
            logger.exception("watch %s check crashed", watch.id)
    return checked


def _is_due(watch: StockWatch, now: datetime) -> bool:
    status = watch.status
    if status == WatchStatus.ACTIVE.value:
        return watch.next_check_at is None or watch.next_check_at <= now
    if status == WatchStatus.FAILED.value:
        return watch.next_retry_at is not None and watch.next_retry_at <= now
    if status == WatchStatus.PAUSED.value:
        return watch.paused_until is not None and watch.paused_until <= now
    return False


def _seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _add_event(
    db: Session,
    watch: StockWatch,
    *,
    kind: str,
    status: str,
    quantity: int | None,
    message: str,
    is_alert: bool = False,
) -> StockWatchEvent:
    event = StockWatchEvent(
        watch_id=watch.id,
        kind=kind,
        status=status,
        quantity=quantity,
        message=message,
        is_alert=is_alert,
    )
    db.add(event)
    return event
