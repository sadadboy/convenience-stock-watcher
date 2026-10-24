"""Restock watcher: poll due watches, record events, raise restock alerts.

Watches never pause on stock. Alerts fire on per-store *changes* only: a store
that newly has stock (🔔 입고), an announced store whose quantity changed
(📉/📈 재고 변동), and an announced store that sold out (⚪ 품절). So silence on
Discord always means "same as the last message", quantities included.
Lookup failures are announced once too, so they can't pass for silence.
Alerts go to the app log, ``stock_watch_events`` (/watches screen) and Discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.domain.alerts import StockStatus, WatchStatus, next_check_time
from app.models.restock import StockWatch, StockWatchEvent
from app.repositories.notifications import get_discord_webhook
from app.services.adapters import check_stock
from app.services.notifications import (
    COLOR_RESTOCK,
    COLOR_SOLD_OUT,
    COLOR_WARNING,
    send_discord_message,
    store_map_link,
)
from app.services.store_adapter import StockLookup, StoreStock

logger = logging.getLogger(__name__)


def _push_discord(db: Session, title: str, message: str, *, color: int, watch_id: int) -> None:
    """Send an alert to Discord if a webhook is configured. Never raises."""
    webhook = get_discord_webhook(db)
    if not webhook:
        return
    ok, error = send_discord_message(webhook, title, message, color=color)
    if not ok:
        logger.warning("Discord push failed for watch %s: %s", watch_id, error)


def _watch_label(watch: StockWatch) -> str:
    source = watch.source
    return f"[{source.brand}] {source.external_product_name}"


def _watch_footer(watch: StockWatch, result: StockLookup | None = None) -> str:
    line = f"상품코드 {watch.source.external_product_code} · 감시 위치: {watch.location_query}"
    if result is not None and result.context:
        line += f" · 조회 범위: {result.context}"
    return line


def load_alerted_stores(watch: StockWatch) -> dict[str, dict]:
    """Announced in-stock stores as {store_code: {"name": str, "qty": int | None}}."""
    try:
        data = json.loads(watch.alerted_stores or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _lookup_stock(watch: StockWatch) -> StockLookup | None:
    """Dispatch to the right brand adapter using the watch's location. None if unsupported."""
    source = watch.source
    return await check_stock(
        source.brand,
        source.external_product_code,
        store_keyword=watch.location_query,
        latitude=watch.latitude,
        longitude=watch.longitude,
        radius_meters=watch.radius_meters,
    )


def _summarize(
    stores: list[StoreStock],
    limit: int = 5,
    *,
    fallback_price: int | None = None,
    link_brand: str | None = None,
) -> str:
    """Render the in-stock stores with their price.

    With ``link_brand`` (Discord only), each store name links to Kakao Map.

    GS25 returns a per-store price; 7-Eleven and Emart24 do not, so those fall
    back to ``fallback_price`` (the price captured when the product code was
    mapped), which for those brands is a fixed nationwide price.
    """
    in_stock = [s for s in stores if s.in_stock]
    parts = []
    for s in in_stock[:limit]:
        name = s.store_name or s.store_code
        if link_brand:
            name = store_map_link(name, s.address, link_brand)
        label = f"{name} {s.quantity}개"
        price = s.price or fallback_price
        if price:
            label += f" ({price:,}원)"
        parts.append(label)
    extra = len(in_stock) - len(parts)
    if extra > 0:
        parts.append(f"외 {extra}곳")
    return ", ".join(parts)


def check_watch(db: Session, watch: StockWatch, *, now: datetime | None = None) -> StockWatchEvent:
    """Run one stock check for a watch, update it, and record an event."""
    now = now or datetime.now(timezone.utc)
    return _apply_result(db, watch, asyncio.run(_lookup_stock(watch)), now=now)


def _apply_result(
    db: Session,
    watch: StockWatch,
    result: StockLookup | None,
    *,
    now: datetime,
) -> StockWatchEvent:
    """Persist one lookup result: update the watch, record an event, alert."""
    source = watch.source

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
        if watch.failure_count == 1:
            retry_min = max(watch.failure_retry_seconds // 60, 1)
            _push_discord(
                db,
                f"⚠️ 조회 실패 {_watch_label(watch)}",
                f"{message}\n{retry_min}분 후 다시 시도합니다. 복구되기 전까지는 재고 알림이 오지 않아요.\n"
                + _watch_footer(watch),
                color=COLOR_WARNING,
                watch_id=watch.id,
            )
        db.commit()
        return event

    recovered = watch.failure_count > 0
    watch.last_error = None
    watch.failure_count = 0
    watch.status = WatchStatus.ACTIVE.value
    watch.next_check_at = next_check_time(check_interval_seconds=watch.check_interval_seconds, now=now)
    watch.paused_until = None
    watch.alert_count = 0

    in_stock_stores = [s for s in result.stores if s.in_stock]
    current = {
        s.store_code: {"name": s.store_name or s.store_code, "qty": s.quantity, "addr": s.address}
        for s in in_stock_stores
    }
    previous = load_alerted_stores(watch)
    appeared = [s for s in in_stock_stores if s.store_code not in previous]
    changed = [
        (s, previous[s.store_code].get("qty"))
        for s in in_stock_stores
        if s.store_code in previous and previous[s.store_code].get("qty") != s.quantity
    ]
    sold_out = [entry.get("name", code) for code, entry in previous.items() if code not in current]
    watch.alerted_stores = json.dumps(current, ensure_ascii=False) if current else None

    total_qty = result.total_quantity
    summary = _summarize(in_stock_stores, fallback_price=source.price) if current else None
    watch.last_stock_status = (StockStatus.IN_STOCK if current else StockStatus.OUT_OF_STOCK).value
    watch.last_stock_quantity = total_qty if current else 0
    watch.last_in_stock_summary = summary

    now_line = (
        f"현재 재고 있는 매장 {len(current)}곳: {summary}"
        if current
        else "현재 재고 있는 매장 없음 · 계속 감시 중"
    )
    footer = _watch_footer(watch, result)
    events: list[StockWatchEvent] = []

    if appeared:
        new_summary = _summarize(appeared, fallback_price=source.price)
        linked_summary = _summarize(appeared, fallback_price=source.price, link_brand=source.brand)
        watch.last_alerted_at = now
        events.append(_add_event(
            db, watch, kind="restock", status="in_stock", quantity=total_qty,
            message=f"입고 감지! 새 매장 {len(appeared)}곳 · {new_summary} ({result.context})",
            is_alert=True,
        ))
        logger.warning(
            "🔔 RESTOCK alert: watch %s (%s %s) -> %s",
            watch.id, source.brand, source.external_product_code, new_summary,
        )
        _push_discord(
            db,
            f"🔔 입고! {_watch_label(watch)}",
            f"새로 입고된 매장 {len(appeared)}곳: {linked_summary}\n{now_line}\n{footer}",
            color=COLOR_RESTOCK,
            watch_id=watch.id,
        )
    if changed:
        decreased = any((s.quantity or 0) < (old or 0) for s, old in changed)
        def change_line(s: StoreStock, old: int | None, name: str) -> str:
            return f"{name} {old if old is not None else '?'}개 → {s.quantity}개"

        lines = ", ".join(change_line(s, old, s.store_name or s.store_code) for s, old in changed)
        linked_lines = ", ".join(
            change_line(s, old, store_map_link(s.store_name or s.store_code, s.address, source.brand))
            for s, old in changed
        )
        watch.last_alerted_at = now
        events.append(_add_event(
            db, watch, kind="quantity", status=watch.last_stock_status, quantity=total_qty,
            message=f"재고 변동: {lines}", is_alert=True,
        ))
        logger.warning("📊 QUANTITY change: watch %s -> %s", watch.id, lines)
        _push_discord(
            db,
            f"{'📉 재고 감소' if decreased else '📈 재고 증가'} {_watch_label(watch)}",
            f"{linked_lines}\n{now_line}\n{footer}",
            color=COLOR_WARNING if decreased else COLOR_RESTOCK,
            watch_id=watch.id,
        )
    if sold_out:
        gone = ", ".join(sold_out)
        watch.last_alerted_at = now
        events.append(_add_event(
            db, watch, kind="sold_out", status=watch.last_stock_status, quantity=total_qty,
            message=f"품절: {gone}", is_alert=True,
        ))
        logger.warning("⚪ SOLD OUT: watch %s -> %s", watch.id, gone)
        _push_discord(
            db,
            f"⚪ 품절 {_watch_label(watch)}",
            f"재고가 사라진 매장: {gone}\n{now_line}\n{footer}",
            color=COLOR_SOLD_OUT,
            watch_id=watch.id,
        )
    if recovered and not events:
        _push_discord(
            db,
            f"✅ 조회 복구 {_watch_label(watch)}",
            f"{now_line}\n{footer}",
            color=COLOR_RESTOCK if current else COLOR_SOLD_OUT,
            watch_id=watch.id,
        )
    if not events:
        message = (
            f"재고 유지 {len(current)}개 매장 · {summary} ({result.context})"
            if current
            else f"재고 없음 (조회 {result.scanned_count}개 매장, {result.context})"
        )
        events.append(_add_event(
            db, watch, kind="check", status=watch.last_stock_status,
            quantity=watch.last_stock_quantity, message=message,
        ))

    db.commit()
    return events[0]


async def _lookup_many(watches: list[StockWatch]) -> list[StockLookup | None | BaseException]:
    """Look up every watch concurrently, bounded by ``watcher_concurrency``.

    The lookups are pure network I/O, so running them one at a time made a tick
    take longer than the poll interval. Exceptions come back in place of a
    result so one bad watch cannot sink the whole tick.
    """
    limit = max(settings.watcher_concurrency, 1)
    semaphore = asyncio.Semaphore(limit)

    async def one(watch: StockWatch) -> StockLookup | None:
        async with semaphore:
            return await _lookup_stock(watch)

    return await asyncio.gather(*(one(w) for w in watches), return_exceptions=True)


def run_due_watches(db: Session, *, now: datetime | None = None) -> int:
    """Check due watches, capped per tick to spread load. Returns the number checked.

    Lookups for the whole batch run concurrently; the results are then applied to
    the DB one at a time, since the Session is not thread-safe.

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
    if not due:
        return 0

    for watch in due:
        if watch.status == WatchStatus.PAUSED.value:
            # Resume a paused watch whose pause window elapsed.
            watch.status = WatchStatus.ACTIVE.value
            watch.paused_until = None
            watch.alert_count = 0

    results = asyncio.run(_lookup_many(due))

    checked = 0
    for watch, result in zip(due, results):
        if isinstance(result, BaseException):
            # Unexpected: adapters already turn network errors into ok=False.
            # Leave the watch untouched so it is retried on the next tick.
            logger.exception("watch %s lookup crashed", watch.id, exc_info=result)
            continue
        try:
            _apply_result(db, watch, result, now=now)
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
