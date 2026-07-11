import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    radius = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class StoreBrand(StrEnum):
    GS25 = "gs25"
    CU = "cu"
    SEVEN_ELEVEN = "seveneleven"
    EMART24 = "emart24"


@dataclass(frozen=True)
class StoreStock:
    """Per-store stock for one product, shared across brand adapters."""

    store_code: str
    store_name: str
    address: str
    quantity: int | None
    in_stock: bool
    price: int | None = None


@dataclass(frozen=True)
class StockLookup:
    """Result of a real-time stock lookup, shared across brand adapters.

    ``ok`` is False when the lookup failed (see ``error``); ``stores`` is then
    empty. ``context`` is a short human-readable description of the query scope
    (coordinates+radius for GS25, store keyword for 7-Eleven).
    """

    checked_at: datetime
    brand: StoreBrand
    item_code: str
    ok: bool
    stores: list[StoreStock] = field(default_factory=list)
    error: str | None = None
    context: str = ""

    @property
    def in_stock_count(self) -> int:
        return sum(1 for store in self.stores if store.in_stock)

    @property
    def total_quantity(self) -> int:
        return sum(store.quantity or 0 for store in self.stores)


@dataclass(frozen=True)
class ProductCandidate:
    brand: StoreBrand
    external_product_code: str
    external_product_name: str
    barcode: str | None = None
    image_url: str | None = None
    price: int | None = None


@dataclass(frozen=True)
class SearchPage:
    """One page of product-search candidates plus whether more pages exist."""

    candidates: list[ProductCandidate]
    page: int = 1
    has_more: bool = False


@dataclass(frozen=True)
class StoreCandidate:
    brand: StoreBrand
    external_store_code: str
    name: str
    address: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class InventoryResult:
    in_stock: bool
    quantity: int | None
    raw_payload: dict


class ConvenienceStoreAdapter(Protocol):
    brand: StoreBrand

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        ...

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        ...

    async def check_inventory(
        self,
        *,
        product_code: str,
        store_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
    ) -> InventoryResult:
        ...
