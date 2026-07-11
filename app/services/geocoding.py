"""Reverse geocoding via OpenStreetMap Nominatim (free, no API key).

Turns a map pin (lat/lon) into a district keyword (예: "동작구") used to seed the
brand store searches. Called only when the user sets a location, so Nominatim's
1 req/sec policy is not a concern. Never raises; returns None on failure.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_HEADERS = {
    "User-Agent": "convenience-stock-watcher/1.0 (personal stock alert app)",
    "Accept-Language": "ko",
}


def _pick_district(address: dict[str, Any]) -> str | None:
    """Pick the best 구/시/군 (then 동) keyword from a Nominatim address dict."""
    # Prefer the gu-level administrative area (borough / city_district / county).
    for key in ("borough", "city_district", "county", "district"):
        value = str(address.get(key) or "").strip()
        if value:
            return value
    # Fall back to any value ending in 구/시/군, then a dong-like field.
    for value in address.values():
        text = str(value or "").strip()
        if text.endswith(("구", "시", "군")):
            return text
    for key in ("suburb", "quarter", "neighbourhood", "town", "city"):
        value = str(address.get(key) or "").strip()
        if value:
            return value
    return None


async def reverse_geocode_keyword(latitude: float, longitude: float) -> str | None:
    """Reverse-geocode a coordinate to a district keyword. Never raises."""
    params = {
        "lat": latitude,
        "lon": longitude,
        "format": "json",
        "accept-language": "ko",
        "zoom": 14,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.get(_NOMINATIM_URL, params=params, headers=_HEADERS)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("reverse geocode network error for (%s,%s): %s", latitude, longitude, exc)
        return None
    except ValueError as exc:
        logger.warning("reverse geocode invalid JSON for (%s,%s): %s", latitude, longitude, exc)
        return None

    address = payload.get("address") if isinstance(payload, dict) else None
    if not isinstance(address, dict):
        return None
    keyword = _pick_district(address)
    logger.info("reverse geocode (%s,%s) -> %r", latitude, longitude, keyword)
    return keyword
