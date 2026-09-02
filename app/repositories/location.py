from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.location import SearchLocation

_SINGLETON_ID = 1


def get_search_location(db: Session) -> SearchLocation:
    """Return the single search-location row, creating it with defaults if absent."""
    location = db.get(SearchLocation, _SINGLETON_ID)
    if location is None:
        location = SearchLocation(
            id=_SINGLETON_ID,
            keyword=settings.seveneleven_default_store_keyword,
            mode="keyword",
            radius_meters=1000,
        )
        db.add(location)
        db.commit()
        db.refresh(location)
    return location


def set_search_location_keyword(db: Session, keyword: str) -> SearchLocation:
    location = get_search_location(db)
    location.keyword = keyword.strip()
    location.mode = "keyword"
    location.latitude = None
    location.longitude = None
    db.commit()
    db.refresh(location)
    return location


def set_search_location_coords(
    db: Session,
    *,
    keyword: str,
    latitude: float,
    longitude: float,
    radius_meters: int,
) -> SearchLocation:
    location = get_search_location(db)
    location.keyword = keyword.strip()
    location.mode = "coords"
    location.latitude = latitude
    location.longitude = longitude
    location.radius_meters = max(radius_meters, 100)
    db.commit()
    db.refresh(location)
    return location


def resolve_location(
    db: Session,
    submitted_keyword: str | None,
) -> tuple[str, float | None, float | None, int | None]:
    """Resolve the effective search location, persisting the keyword as last-used.

    Returns (keyword, latitude, longitude, radius_meters). Coordinates are only
    returned when the saved location is in coords mode and the submitted keyword
    still matches it (i.e. the user hasn't typed a different area).
    """
    location = get_search_location(db)
    keyword = (submitted_keyword or "").strip()

    if not keyword:
        if location.mode == "coords":
            return location.keyword, location.latitude, location.longitude, location.radius_meters
        return location.keyword, None, None, None

    if location.mode == "coords" and keyword == location.keyword:
        return keyword, location.latitude, location.longitude, location.radius_meters

    if keyword != location.keyword or location.mode != "keyword":
        set_search_location_keyword(db, keyword)
    return keyword, None, None, None
