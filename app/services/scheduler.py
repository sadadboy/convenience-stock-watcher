"""APScheduler background job that polls due restock watches."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.watcher import run_due_watches

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _tick() -> None:
    db = SessionLocal()
    try:
        checked = run_due_watches(db)
        if checked:
            logger.info("watcher tick: checked %d watch(es)", checked)
    except Exception:  # noqa: BLE001
        logger.exception("watcher tick failed")
    finally:
        db.close()


def start_scheduler() -> None:
    global _scheduler
    if not settings.watcher_enabled:
        logger.info("watcher disabled (WATCHER_ENABLED=false); scheduler not started")
        return
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick,
        "interval",
        seconds=settings.watcher_poll_seconds,
        id="watcher_tick",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("watcher scheduler started (poll every %ss)", settings.watcher_poll_seconds)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
