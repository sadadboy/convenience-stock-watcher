"""Restock watcher: poll due watches, record events, raise restock alerts.

Watches never pause on stock. Alerts fire on per-store *changes* only: a store
that newly has stock (🔔 입고), an announced store whose quantity changed
(📉/📈 재고 변동), and an announced store that sold out (⚪ 품절). So silence on
Discord always means "same as the last message", quantities included.
Lookup failures can't pass for silence either, but blips must not spam: a
failing watch retries with backoff (5 -> 10 -> 20 -> ... -> failure_retry_seconds)
and is announced only once it has failed ``FAILURE_ALERT_AFTER`` times in a row
(~15 min). Failures and recoveries from one scheduler tick are grouped into a
single Discord message per brand. Watches still failing show up in the daily
report. Alerts go to the app log, ``stock_watch_events`` and Discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    brand_display_name,
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


# Consecutive failures before a watch's failure is announced. With 5-min checks
# the retries land at +5 and +15 min, so this is "still failing after ~15 min".
FAILURE_ALERT_AFTER = 3


def failure_retry_delay(watch: StockWatch) -> timedelta:
    """Backoff after the Nth consecutive failure: interval * 2^(N-1), capped."""
    base = max(watch.check_interval_seconds, 60)
    exponent = min(max(watch.failure_count - 1, 0), 10)
    return timedelta(seconds=min(base * 2**exponent, max(watch.failure_retry_seconds, base)))


@dataclass
class _Notice:
    watch_id: int
    brand: str
    label: str
    error: str | None
    at: datetime


class HealthBatch:
    """Failure / recovery notices, held briefly and sent grouped by brand.

    One outage hits every watch of a brand, but their checks are spread over a
    whole check interval, so they cross the threshold in different ticks. A
    brand's notices are held until the first one is ``hold`` old, then sent as
    one message. Plain strings only: the watches are detached by then.
    """

    def __init__(self, hold: timedelta = timedelta(0)) -> None:
        self.hold = hold
        self.failed: dict[str, list[_Notice]] = defaultdict(list)
        self.recovered: dict[str, list[_Notice]] = defaultdict(list)

    def add_failed(self, watch: StockWatch, error: str, now: datetime) -> None:
        self.failed[watch.source.brand].append(_Notice(watch.id, watch.source.brand, _short_label(watch), error, now))

    def add_recovered(self, watch: StockWatch, now: datetime) -> None:
        label = _short_label(watch)
        pending = self.failed.get(watch.source.brand, [])
        if any(n.watch_id == watch.id for n in pending):
            # Failure not sent yet: it recovered within the hold, say nothing.
            self.failed[watch.source.brand] = [n for n in pending if n.watch_id != watch.id]
            return
        self.recovered[watch.source.brand].append(_Notice(watch.id, watch.source.brand, label, None, now))

    def _ready(self, notices: list[_Notice], now: datetime) -> bool:
        return bool(notices) and now - min(n.at for n in notices) >= self.hold

    def flush(self, db: Session, now: datetime) -> None:
        for brand in list(self.failed):
            items = self.failed[brand]
            if not self._ready(items, now):
                continue
            by_error: dict[str, list[str]] = defaultdict(list)
            for n in items:
                by_error[n.error or "재고 조회 실패"].append(n.label)
            lines = []
            for error, labels in by_error.items():
                lines.append(f"**{error}** · 감시 {len(labels)}개")
                lines += _cap_lines(labels)
            lines.append(
                "\n15분 넘게 계속 실패 중이에요. 점점 간격을 늘려 최대 1시간마다 재시도하고, "
                "복구되기 전까지는 재고 알림이 오지 않아요. 계속 실패하면 매일 09시 보고에 남아요."
            )
            _push_discord(
                db,
                f"⚠️ {brand_display_name(brand)} 조회 실패 · 감시 {len(items)}개",
                "\n".join(lines),
                color=COLOR_WARNING,
                watch_id=0,
            )
            del self.failed[brand]
        for brand in list(self.recovered):
            items = self.recovered[brand]
            if not self._ready(items, now):
                continue
            _push_discord(
                db,
                f"✅ {brand_display_name(brand)} 조회 복구 · 감시 {len(items)}개",
                "\n".join(_cap_lines([n.label for n in items]) + ["\n변동이 있으면 평소처럼 알림이 와요."]),
                color=COLOR_RESTOCK,
                watch_id=0,
            )
            del self.recovered[brand]


# Shared across scheduler ticks (a single scheduler thread runs them).
_tick_notices = HealthBatch(hold=timedelta(seconds=settings.watcher_default_interval_seconds))


def _short_label(watch: StockWatch) -> str:
    return f"{watch.source.external_product_name} ({watch.location_query or '전국'})"


def _cap_lines(labels: list[str], limit: int = 10) -> list[str]:
    lines = [f"• {label}" for label in labels[:limit]]
    if len(labels) > limit:
        lines.append(f"… 외 {len(labels) - limit}개")
    return lines


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
    # Manual check: report its own failure/recovery right away.
    batch = HealthBatch()
    event = _apply_result(db, watch, asyncio.run(_lookup_stock(watch)), now=now, batch=batch)
    batch.flush(db, now)
    return event


def _apply_result(
    db: Session,
    watch: StockWatch,
    result: StockLookup | None,
    *,
    now: datetime,
    batch: HealthBatch,
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
        watch.next_retry_at = now + failure_retry_delay(watch)
        event = _add_event(db, watch, kind="error", status="failed", quantity=None, message=message)
        logger.warning("watch %s failed (%d in a row): %s", watch.id, watch.failure_count, message)
        if watch.failure_count == FAILURE_ALERT_AFTER:
            batch.add_failed(watch, message, now)
        db.commit()
        return event

    # Only announce a recovery if the failure itself was announced.
    recovered = watch.failure_count >= FAILURE_ALERT_AFTER
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
        batch.add_recovered(watch, now)
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
    batch = _tick_notices
    if not due:
        batch.flush(db, now)
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
            _apply_result(db, watch, result, now=now, batch=batch)
            checked += 1
        except Exception:  # noqa: BLE001 - never let one watch break the loop
            db.rollback()
            logger.exception("watch %s check crashed", watch.id)
    batch.flush(db, now)
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
