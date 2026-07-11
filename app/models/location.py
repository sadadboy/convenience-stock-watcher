"""Persisted "current search location" (single global row).

Holds the last-used stock-search location so the /products screen doesn't reset
to a hardcoded default. ``mode`` is either:

- ``keyword``: search stores by ``keyword`` text only.
- ``coords``: seed the store fetch with ``keyword`` (a 구/동 from reverse
  geocoding a map pin), then filter to stores within ``radius_meters`` of
  ``latitude``/``longitude``.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SearchLocation(Base):
    __tablename__ = "search_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="keyword")
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    radius_meters: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
