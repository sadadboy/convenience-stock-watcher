"""Device-based login: password once per device, then a long-lived cookie.

The background watcher runs inside the app process and never goes through
HTTP, so none of this affects scheduled stock checks.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.device import TrustedDevice

COOKIE_NAME = "csw_device"
# Browsers cap cookie lifetime at 400 days; we refresh it while the device is used.
_REFRESH_AFTER = timedelta(days=1)

# Failed password attempts per client IP (in memory; resets on restart).
_FAIL_WINDOW_SECONDS = 15 * 60
_FAIL_LIMIT = 5
_failures: dict[str, deque[float]] = defaultdict(deque)


def auth_configured() -> bool:
    return bool(settings.auth_password)


def cookie_max_age() -> int:
    return min(settings.auth_device_days, 400) * 86400


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _recent_failures(ip: str) -> deque[float]:
    attempts = _failures[ip]
    cutoff = time.monotonic() - _FAIL_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


def is_locked_out(ip: str) -> bool:
    return len(_recent_failures(ip)) >= _FAIL_LIMIT


def check_password(ip: str, password: str) -> bool:
    """Constant-time password check that counts failures per IP."""
    if not auth_configured() or is_locked_out(ip):
        return False
    ok = hmac.compare_digest(password.encode(), settings.auth_password.encode())
    if ok:
        _failures.pop(ip, None)
    else:
        _recent_failures(ip).append(time.monotonic())
    return ok


def register_device(db: Session, *, name: str, user_agent: str, ip: str) -> str:
    """Create a trusted device and return its raw cookie token."""
    token = secrets.token_urlsafe(32)
    db.add(
        TrustedDevice(
            name=name.strip()[:100] or "이름 없는 기기",
            token_hash=_hash(token),
            user_agent=user_agent[:400],
            created_ip=ip[:64],
            last_ip=ip[:64],
        )
    )
    db.commit()
    return token


def find_device(db: Session, token: str | None) -> TrustedDevice | None:
    if not token:
        return None
    return db.scalar(select(TrustedDevice).where(TrustedDevice.token_hash == _hash(token)))


def touch_device(db: Session, device: TrustedDevice, ip: str) -> bool:
    """Record use at most once a day. True when the cookie should be refreshed."""
    now = datetime.now(timezone.utc)
    last_seen = device.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if now - last_seen < _REFRESH_AFTER:
        return False
    device.last_seen_at = now
    device.last_ip = ip[:64]
    db.commit()
    return True


def guess_device_name(user_agent: str) -> str:
    ua = user_agent.lower()
    for needle, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("macintosh", "Mac"),
        ("windows", "Windows PC"),
        ("linux", "Linux"),
    ):
        if needle in ua:
            return label
    return "내 기기"


def safe_next(path: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"
