"""Record /login attempts and alert on failures over Discord.

Never raises into the login flow: a broken audit must not lock the owner out.
"""

from __future__ import annotations

import difflib
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.login_attempt import LoginAttempt
from app.repositories.notifications import get_discord_webhook
from app.services.notifications import COLOR_WARNING, send_discord_message

logger = logging.getLogger(__name__)

RETENTION_DAYS = 180
SIMILARITY_THRESHOLD = 0.7  # 0..1, difflib ratio against the real password
SIMILAR_NOTE = "similar_to_real"

# Discord alert throttle: at most one alert per IP per hour (in memory).
_ALERT_INTERVAL_SECONDS = 3600
_last_alert: dict[str, float] = {}
_alert_lock = threading.Lock()


def _resembles_real_password(attempt: str) -> bool:
    real = settings.auth_password or ""
    if not real or not attempt:
        return False
    a, r = attempt.strip().lower(), real.strip().lower()
    if a == r or (len(a) >= 4 and (a in r or r in a)):
        return True
    return difflib.SequenceMatcher(None, a, r).ratio() >= SIMILARITY_THRESHOLD


def record_attempt(
    db: Session,
    *,
    ip: str,
    user_agent: str,
    result: str,
    password: str | None,
    device_name: str = "",
) -> None:
    try:
        stored, note = None, None
        if result != "success" and password is not None:
            if _resembles_real_password(password):
                note = SIMILAR_NOTE
            else:
                stored = password[:200]
        db.add(
            LoginAttempt(
                ip=ip[:64],
                user_agent=user_agent[:400],
                result=result,
                attempted_password=stored,
                password_note=note,
                device_name=(device_name or "")[:100],
            )
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
        db.commit()
    except Exception:
        logger.exception("login attempt audit failed")
        db.rollback()
        return

    if result != "success":
        _maybe_alert(ip, user_agent, result, similar=note == SIMILAR_NOTE)


def _maybe_alert(ip: str, user_agent: str, result: str, *, similar: bool) -> None:
    now = time.monotonic()
    with _alert_lock:
        if now - _last_alert.get(ip, -_ALERT_INTERVAL_SECONDS) < _ALERT_INTERVAL_SECONDS:
            return
        _last_alert[ip] = now
        for k in [k for k, t in _last_alert.items() if now - t >= _ALERT_INTERVAL_SECONDS]:
            if k != ip:
                _last_alert.pop(k, None)
    # Send off the request thread so a slow Discord never delays the login page.
    threading.Thread(
        target=_send_alert, args=(ip, user_agent, result, similar), daemon=True
    ).start()


def _send_alert(ip: str, user_agent: str, result: str, similar: bool) -> None:
    db = SessionLocal()
    try:
        webhook = get_discord_webhook(db)
    finally:
        db.close()
    if not webhook:
        return
    label = {"failed": "비밀번호 틀림", "locked": "잠금 중 재시도"}.get(result, result)
    lines = [
        f"- IP: `{ip}` (https://ipinfo.io/{ip})",
        f"- 결과: {label}",
        f"- 기기: {user_agent[:150] or '(없음)'}",
    ]
    if similar:
        lines.append("- 입력값이 실제 비밀번호와 비슷함 (본인 오타일 가능성)")
    lines.append("- 같은 IP 알림은 1시간에 한 번만 보냅니다. 전체 기록: `/login-attempts`")
    # The attempted password is deliberately not sent to Discord (third party).
    send_discord_message(webhook, "🔐 로그인 실패 감지", "\n".join(lines), color=COLOR_WARNING)
