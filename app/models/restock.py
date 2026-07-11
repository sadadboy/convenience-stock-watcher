"""Restock watcher models.

A ``StockWatch`` is one thing to monitor: a saved product source (brand +
product code) plus a location query (a store-search keyword for 7-Eleven /
Emart24, or ``"lat,lon,radius"`` for GS25). The scheduler polls due watches,
records ``StockWatchEvent`` rows, and raises a restock alert when stock appears.

This is intentionally separate from the store-centric ``watch_rules`` /
``watch_targets`` models: the working brand adapters discover stores by keyword
at check time rather than from pre-registered ``stores`` rows.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class StockWatch(Base):
    __tablename__ = "stock_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_source_id: Mapped[int] = mapped_column(
        ForeignKey("product_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Store-search keyword (7-Eleven / Emart24 seed; also GS25 display).
    location_query: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional map location: when set, stores are filtered to within radius.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    radius_meters: Mapped[int | None] = mapped_column(Integer)

    check_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    max_alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    pause_after_alerts_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=18000)
    failure_retry_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)

    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    last_stock_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    last_stock_quantity: Mapped[int | None] = mapped_column(Integer)
    last_in_stock_summary: Mapped[str | None] = mapped_column(Text)

    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    source: Mapped["ProductSource"] = relationship("ProductSource")
    events: Mapped[list["StockWatchEvent"]] = relationship(
        back_populates="watch",
        cascade="all, delete-orphan",
        order_by="StockWatchEvent.created_at.desc()",
    )


class StockWatchEvent(Base):
    __tablename__ = "stock_watch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("stock_watches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "restock" (alert), "check", "error".
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_alert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    watch: Mapped[StockWatch] = relationship(back_populates="events")
