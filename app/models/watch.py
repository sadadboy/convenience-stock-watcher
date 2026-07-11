from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class WatchRule(Base):
    __tablename__ = "watch_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False, default="radius")
    center_latitude: Mapped[float | None] = mapped_column(Float)
    center_longitude: Mapped[float | None] = mapped_column(Float)
    radius_meters: Mapped[int | None] = mapped_column(Integer)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    pause_after_alerts_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=18000)
    failure_retry_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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

    brands: Mapped[list["WatchRuleBrand"]] = relationship(
        back_populates="watch_rule",
        cascade="all, delete-orphan",
    )
    stores: Mapped[list["WatchRuleStore"]] = relationship(
        back_populates="watch_rule",
        cascade="all, delete-orphan",
    )
    targets: Mapped[list["WatchTarget"]] = relationship(
        back_populates="watch_rule",
        cascade="all, delete-orphan",
    )


class WatchRuleBrand(Base):
    __tablename__ = "watch_rule_brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_rule_id: Mapped[int] = mapped_column(
        ForeignKey("watch_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    brand: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    watch_rule: Mapped[WatchRule] = relationship(back_populates="brands")


class WatchRuleStore(Base):
    __tablename__ = "watch_rule_stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_rule_id: Mapped[int] = mapped_column(
        ForeignKey("watch_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)

    watch_rule: Mapped[WatchRule] = relationship(back_populates="stores")


class WatchTarget(Base):
    __tablename__ = "watch_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_rule_id: Mapped[int] = mapped_column(ForeignKey("watch_rules.id"), nullable=False, index=True)
    product_source_id: Mapped[int] = mapped_column(
        ForeignKey("product_sources.id"),
        nullable=False,
        index=True,
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    last_stock_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    last_stock_quantity: Mapped[int | None] = mapped_column(Integer)
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

    watch_rule: Mapped[WatchRule] = relationship(back_populates="targets")
    store: Mapped["Store"] = relationship(back_populates="watch_targets")
    snapshots: Mapped[list["StockSnapshot"]] = relationship(
        back_populates="watch_target",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[list["Alert"]] = relationship(back_populates="watch_target")


class StockSnapshot(Base):
    __tablename__ = "stock_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_target_id: Mapped[int] = mapped_column(
        ForeignKey("watch_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    watch_target: Mapped[WatchTarget] = relationship(back_populates="snapshots")
