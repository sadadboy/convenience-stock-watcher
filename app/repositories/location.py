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
