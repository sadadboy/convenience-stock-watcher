"""Discord webhook push notifications.

Synchronous (uses httpx.Client) because it is called from the watcher, which
runs in a background scheduler thread. Never raises; returns (ok, error).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_USERNAME = "편의점 재고 알리미"
_COLOR_RESTOCK = 0x2ECC71  # green
_TIMEOUT = 10.0


def send_discord_message(
    webhook_url: str,
    title: str,
    message: str,
    *,
    color: int = _COLOR_RESTOCK,
) -> tuple[bool, str | None]:
    """Post an embed to a Discord webhook. Returns (ok, error_message)."""
    url = (webhook_url or "").strip()
    if not url:
        return False, "Discord 웹훅 URL이 설정되지 않았습니다."

    payload = {
        "username": _USERNAME,
        "embeds": [
            {
                "title": title[:256],
                "description": message[:4000],
                "color": color,
            }
        ],
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(url, json=payload)
        if response.status_code < 300:
            return True, None
        logger.warning("Discord send failed: HTTP %s %s", response.status_code, response.text[:200])
        return False, f"Discord 응답 오류 (HTTP {response.status_code})"
    except httpx.HTTPError as exc:
        logger.warning("Discord send network error: %s", exc)
        return False, f"Discord 전송 네트워크 오류: {exc}"
