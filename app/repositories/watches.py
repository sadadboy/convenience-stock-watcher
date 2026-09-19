from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.domain.alerts import WatchStatus
from app.models.product import Product, ProductSource
from app.models.restock import StockWatch, StockWatchEvent


def list_watches(db: Session) -> list[StockWatch]:
    return list(
        db.scalars(
            select(StockWatch)
            .options(selectinload(StockWatch.source).selectinload(ProductSource.product))
            .order_by(StockWatch.created_at.desc())
        )
    )


def get_watch(db: Session, watch_id: int) -> StockWatch | None:
    return db.scalar(
        select(StockWatch)
        .options(selectinload(StockWatch.source).selectinload(ProductSource.product))
        .where(StockWatch.id == watch_id)
    )


def list_watchable_sources(db: Session) -> list[ProductSource]:
    """Enabled product sources, with their product, for the create dropdown."""
    return list(
        db.scalars(
            select(ProductSource)
            .options(joinedload(ProductSource.product))
            .where(ProductSource.enabled.is_(True))
            .order_by(ProductSource.created_at.desc())
        )
    )


LOCATION_KEY_NEW = "__new__"


def encode_location_key(
    location_query: str,
    latitude: float | None,
    longitude: float | None,
    radius_meters: int | None,
) -> str:
    """Pack a location into one form value ("lat|lon|radius|keyword").

    The keyword goes last so it can contain "|" without breaking the split.
    """
    parts = ["" if v is None else str(v) for v in (latitude, longitude, radius_meters)]
    return "|".join(parts + [location_query])


def decode_location_key(key: str) -> tuple[str, float | None, float | None, int | None] | None:
    """Unpack encode_location_key; None if the value isn't a location key."""
    parts = key.split("|", 3)
    if len(parts) != 4:
        return None
    lat, lon, radius, keyword = parts
    try:
        return (
            keyword,
            float(lat) if lat else None,
            float(lon) if lon else None,
            int(radius) if radius else None,
        )
    except ValueError:
        return None


def list_watchable_products(db: Session) -> list[Product]:
    """Products with at least one enabled source, for the product-level add form."""
    return list(
        db.scalars(
            select(Product)
            .options(selectinload(Product.sources))
            .join(ProductSource, ProductSource.product_id == Product.id)
            .where(ProductSource.enabled.is_(True))
            .distinct()
            .order_by(Product.display_name)
        )
    )


def create_watch(
    db: Session,
    *,
    product_source_id: int,
    location_query: str,
    check_interval_seconds: int,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockWatch | None:
    source = db.get(ProductSource, product_source_id)
    if source is None:
        return None
    watch = StockWatch(
        product_source_id=product_source_id,
        location_query=location_query.strip(),
        check_interval_seconds=check_interval_seconds,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        status=WatchStatus.ACTIVE.value,
        next_check_at=datetime.now(timezone.utc),  # check on the next tick
    )
    db.add(watch)
    db.commit()
    db.refresh(watch)
    return watch


def create_watches_for_product(
    db: Session,
    *,
    product_id: int,
    location_query: str,
    check_interval_seconds: int,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> int:
    """Create a watch for every enabled source of a product not already watched.

    Returns the number of watches created.
    """
    sources = list(
        db.scalars(
            select(ProductSource)
            .where(ProductSource.product_id == product_id)
            .where(ProductSource.enabled.is_(True))
        )
    )
    if not sources:
        return 0

    keyword = location_query.strip()
    # A source may be watched at multiple locations; only skip the *same* location.
    # Map locations are identified by their coordinates, so a watch whose label
    # was renamed (e.g. "동작구" -> "동작구(상도)") still counts as the same spot.
    if latitude is not None and longitude is not None:
        target_key = (latitude, longitude, radius_meters)

        def location_key(w: StockWatch):
            return (w.latitude, w.longitude, w.radius_meters)
    else:
        target_key = (keyword, None, None, None)

        def location_key(w: StockWatch):
            return (w.location_query, w.latitude, w.longitude, w.radius_meters)

    existing = db.scalars(
        select(StockWatch).where(
            StockWatch.product_source_id.in_([s.id for s in sources])
        )
    )
    already = {w.product_source_id for w in existing if location_key(w) == target_key}
    now = datetime.now(timezone.utc)
    to_create = [s for s in sources if s.id not in already]
    # Spread first checks evenly across the interval so they don't burst together.
    step = check_interval_seconds / len(to_create) if to_create else 0
    for i, source in enumerate(to_create):
        db.add(
            StockWatch(
                product_source_id=source.id,
                location_query=keyword,
                check_interval_seconds=check_interval_seconds,
                latitude=latitude,
                longitude=longitude,
                radius_meters=radius_meters,
                status=WatchStatus.ACTIVE.value,
                next_check_at=now + timedelta(seconds=step * i),
            )
        )
    db.commit()
    return len(to_create)


def watches_for_product(db: Session, product_id: int) -> list[StockWatch]:
    return list(
        db.scalars(
            select(StockWatch)
            .options(selectinload(StockWatch.source).selectinload(ProductSource.product))
            .join(ProductSource, StockWatch.product_source_id == ProductSource.id)
            .where(ProductSource.product_id == product_id)
        )
    )


def _tally(watches: list[StockWatch]) -> dict:
    """Counters + bulk-action payload shared by the location and product levels."""
    return {
        "total": len(watches),
        "in_stock": sum(1 for w in watches if w.last_stock_status == "in_stock"),
        "active": sum(1 for w in watches if w.status == "active"),
        "failed": sum(1 for w in watches if w.status == "failed"),
        "paused": sum(1 for w in watches if w.status == "paused"),
        "watch_ids": ",".join(str(w.id) for w in watches),
        "interval": watches[0].check_interval_seconds,
        "max_alert_count": watches[0].max_alert_count,
    }


def list_watch_locations(db: Session) -> list[dict]:
    """Group watches by location, then by product, for the /watches UI.

    Location comes first because that is how watches are actually managed: one
    area is checked as a unit, and several products share it.
    """
    by_location: dict[tuple, dict] = {}
    for watch in list_watches(db):
        loc_key = (watch.location_query, watch.latitude, watch.longitude, watch.radius_meters)
        location = by_location.get(loc_key)
        if location is None:
            location = {
                "key": encode_location_key(*loc_key),
                "location_query": watch.location_query,
                "latitude": watch.latitude,
                "longitude": watch.longitude,
                "radius_meters": watch.radius_meters,
                "has_coords": watch.latitude is not None,
                "watches": [],
                "products": {},
            }
            by_location[loc_key] = location
        location["watches"].append(watch)
        product = watch.source.product
        location["products"].setdefault(product.id, {"product": product, "watches": []})
        location["products"][product.id]["watches"].append(watch)

    result = []
    for location in by_location.values():
        location.update(_tally(location["watches"]))
        products = []
        for entry in location["products"].values():
            entry.update(_tally(entry["watches"]))
            products.append(entry)
        products.sort(key=lambda p: p["product"].display_name)
        location["products"] = products
        result.append(location)
    # Nationwide watches (empty location) last; the rest by area name.
    result.sort(key=lambda loc: (loc["location_query"] == "", loc["location_query"]))
    return result


def _parse_ids(watch_ids: str) -> list[int]:
    return [int(x) for x in watch_ids.split(",") if x.strip().isdigit()]


def set_watches_status(db: Session, watch_ids: str, status: WatchStatus) -> int:
    ids = _parse_ids(watch_ids)
    if not ids:
        return 0
    watches = list(db.scalars(select(StockWatch).where(StockWatch.id.in_(ids))))
    for watch in watches:
        watch.status = status.value
        if status == WatchStatus.ACTIVE:
            watch.paused_until = None
            watch.next_retry_at = None
            watch.alert_count = 0
            watch.next_check_at = datetime.now(timezone.utc)
    db.commit()
    return len(watches)


def delete_watches(db: Session, watch_ids: str) -> int:
    ids = _parse_ids(watch_ids)
    if not ids:
        return 0
    watches = list(db.scalars(select(StockWatch).where(StockWatch.id.in_(ids))))
    for watch in watches:
        db.delete(watch)
    db.commit()
    return len(watches)


def update_watches_settings(
    db: Session,
    watch_ids: str,
    *,
    check_interval_seconds: int,
    max_alert_count: int,
) -> int:
    """Update interval / max-alert for a group of watches, re-staggering checks."""
    ids = _parse_ids(watch_ids)
    if not ids:
        return 0
    interval = max(check_interval_seconds, 30)
    max_alerts = max(max_alert_count, 1)
    watches = list(db.scalars(select(StockWatch).where(StockWatch.id.in_(ids))))
    now = datetime.now(timezone.utc)
    step = interval / len(watches) if watches else 0
    for i, watch in enumerate(watches):
        watch.check_interval_seconds = interval
        watch.max_alert_count = max_alerts
        # Re-spread the next checks across the new interval.
        watch.next_check_at = now + timedelta(seconds=step * i)
    db.commit()
    return len(watches)


def set_watch_status(db: Session, watch_id: int, status: WatchStatus) -> StockWatch | None:
    watch = db.get(StockWatch, watch_id)
    if watch is None:
        return None
    watch.status = status.value
    if status == WatchStatus.ACTIVE:
        # Resume: clear pause/failure timers and check on the next tick.
        watch.paused_until = None
        watch.next_retry_at = None
        watch.alert_count = 0
        watch.next_check_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(watch)
    return watch


def delete_watch(db: Session, watch_id: int) -> bool:
    watch = db.get(StockWatch, watch_id)
    if watch is None:
        return False
    db.delete(watch)
    db.commit()
    return True


def recent_events(db: Session, limit: int = 40) -> list[StockWatchEvent]:
    return list(
        db.scalars(
            select(StockWatchEvent)
            .options(
                selectinload(StockWatchEvent.watch)
                .selectinload(StockWatch.source)
                .selectinload(ProductSource.product)
            )
            .order_by(StockWatchEvent.created_at.desc())
            .limit(limit)
        )
    )
