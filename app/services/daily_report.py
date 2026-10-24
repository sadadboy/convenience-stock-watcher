"""Daily Discord heartbeat: proves the watcher is alive and restates the state.

Restock alerts only fire on changes, so a dead server would look exactly like
"nothing changed". This report arrives every day at ``daily_report_hour`` KST;
if it doesn't, the server (not the stock) is the problem.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.domain.alerts import WatchStatus
from app.models.restock import StockWatch
from app.repositories.notifications import get_discord_webhook
from app.services.notifications import (
    COLOR_RESTOCK,
    COLOR_WARNING,
    send_discord_message,
    store_map_link,
)
from app.services.watcher import load_alerted_stores

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
_MAX_LINES = 15


def _is_stale(watch: StockWatch, now: datetime) -> bool:
    """An active watch that hasn't been checked in 3 intervals (min 15 min)."""
    if watch.last_checked_at is None:
        return True
    checked = watch.last_checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    grace = timedelta(seconds=max(watch.check_interval_seconds * 3, 900))
    return now - checked > grace


def _label(watch: StockWatch) -> str:
    return f"[{watch.source.brand}] {watch.source.external_product_name} ({watch.location_query or '전국'})"


def _cap(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_LINES:
        return lines
    return lines[:_MAX_LINES] + [f"… 외 {len(lines) - _MAX_LINES}개"]


def build_daily_report(db: Session, *, now: datetime | None = None) -> tuple[str, str, int]:
    """Return (title, message, color) describing every watch's current state."""
    now = now or datetime.now(timezone.utc)
    watches = db.scalars(select(StockWatch).options(selectinload(StockWatch.source))).all()

    disabled = [w for w in watches if w.status == WatchStatus.DISABLED.value]
    failed = [w for w in watches if w.status == WatchStatus.FAILED.value]
    running = [w for w in watches if w.status == WatchStatus.ACTIVE.value]
    stale = [w for w in running if _is_stale(w, now)]

    in_stock_lines = []
    for w in running:
        stores = load_alerted_stores(w)
        if not stores:
            continue
        parts = [
            f"{store_map_link(e.get('name', code), e.get('addr'), w.source.brand)} {e.get('qty')}개"
            for code, e in stores.items()
        ]
        in_stock_lines.append(f"• {_label(w)}: {', '.join(parts[:5])}" + (f" 외 {len(parts) - 5}곳" if len(parts) > 5 else ""))

    problems = bool(failed or stale)
    title = "⚠️ 감시 상태 확인 필요" if problems else "✅ 감시 정상 동작 중"
    lines = [
        f"{now.astimezone(KST):%m-%d %H:%M} 기준 · 감시 {len(watches)}개 "
        f"(정상 {len(running) - len(stale)} · 실패 {len(failed)} · 지연 {len(stale)} · 중지 {len(disabled)})",
        "",
        "**재고 있는 곳**",
        *(_cap(in_stock_lines) or ["없음 · 계속 감시 중"]),
    ]
    if failed:
        lines += ["", "**조회 실패** (재시도 중, 그동안 재고 알림 없음)"]
        lines += _cap([f"• {_label(w)}: {w.last_error or '알 수 없는 오류'}" for w in failed])
    if stale:
        lines += ["", "**조회 지연** (예정보다 오래 조회 안 됨)"]
        lines += _cap([
            f"• {_label(w)}: 마지막 조회 "
            + (f"{w.last_checked_at.replace(tzinfo=w.last_checked_at.tzinfo or timezone.utc).astimezone(KST):%m-%d %H:%M}" if w.last_checked_at else "없음")
            for w in stale
        ])
    lines += ["", "이 메시지가 매일 안 오면 서버를 확인하세요."]
    return title, "\n".join(lines), COLOR_WARNING if problems else COLOR_RESTOCK


def send_daily_report(db: Session) -> bool:
    """Build and post the daily report. Never raises; returns whether it was sent."""
    webhook = get_discord_webhook(db)
    if not webhook:
        return False
    title, message, color = build_daily_report(db)
    ok, error = send_discord_message(webhook, title, message, color=color)
    if not ok:
        logger.warning("daily report push failed: %s", error)
    return ok
