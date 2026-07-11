import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.notification import NotificationChannel

_DISCORD_TYPE = "discord"


def _get_discord_channel(db: Session) -> NotificationChannel | None:
    return db.scalar(
        select(NotificationChannel).where(NotificationChannel.type == _DISCORD_TYPE)
    )


def get_discord_webhook(db: Session) -> str | None:
    """Return the active Discord webhook URL (DB first, then .env fallback)."""
    channel = _get_discord_channel(db)
    if channel is not None and channel.enabled:
        try:
            url = (json.loads(channel.config_json) or {}).get("webhook_url", "")
        except (ValueError, TypeError):
            url = ""
        if url.strip():
            return url.strip()
    return settings.discord_webhook_url.strip() or None


def set_discord_webhook(db: Session, webhook_url: str) -> NotificationChannel:
    """Upsert the single Discord channel. Empty URL disables it."""
    url = (webhook_url or "").strip()
    channel = _get_discord_channel(db)
    if channel is None:
        channel = NotificationChannel(type=_DISCORD_TYPE, name="Discord", config_json="{}")
        db.add(channel)
    channel.config_json = json.dumps({"webhook_url": url})
    channel.enabled = bool(url)
    db.commit()
    db.refresh(channel)
    return channel
