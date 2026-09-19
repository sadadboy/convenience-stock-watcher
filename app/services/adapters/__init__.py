"""Retail brand adapters and a small registry.

Each brand adapter exposes at least ``search_products``. Use ``search_products``
(single page) or ``search_products_page`` (paged) to run a brand-agnostic
candidate search from the API layer.
"""

from __future__ import annotations

from app.services.adapters.cu import CUAdapter, check_cu_stock, search_cu_products
from app.services.adapters.emart24 import (
    Emart24Adapter,
    check_emart24_stock,
    search_emart24_products,
)
from app.services.adapters.gs25 import GS25Adapter, check_gs25_stock, search_gs25_products
from app.services.adapters.seven_eleven import (
    SevenElevenAdapter,
    check_seveneleven_stock,
    search_seveneleven_products,
)
from app.services.store_adapter import ProductCandidate, SearchPage, StockLookup, StoreBrand

# Brands with a working product-name search adapter.
_SEARCH_ADAPTERS = {
    StoreBrand.GS25: GS25Adapter(),
    StoreBrand.CU: CUAdapter(),
    StoreBrand.SEVEN_ELEVEN: SevenElevenAdapter(),
    StoreBrand.EMART24: Emart24Adapter(),
}

# Paged search functions per brand.
_SEARCH_PAGE_FUNCS = {
    StoreBrand.GS25: search_gs25_products,
    StoreBrand.CU: search_cu_products,
    StoreBrand.SEVEN_ELEVEN: search_seveneleven_products,
    StoreBrand.EMART24: search_emart24_products,
}

# Ordered list of brands that support candidate search (for UI selectors).
SEARCH_BRANDS = list(_SEARCH_ADAPTERS.keys())


def supports_search(brand: str | StoreBrand) -> bool:
    try:
        return StoreBrand(brand) in _SEARCH_ADAPTERS
    except ValueError:
        return False


async def search_products(brand: str | StoreBrand, keyword: str) -> list[ProductCandidate]:
    """Search the first page of candidates for a brand. Returns [] for unknown brands."""
    page = await search_products_page(brand, keyword, page=1)
    return page.candidates


async def search_products_page(brand: str | StoreBrand, keyword: str, page: int = 1) -> SearchPage:
    """Search one page of candidates for a brand. Empty page for unknown brands."""
    try:
        resolved = StoreBrand(brand)
    except ValueError:
        return SearchPage(candidates=[], page=page, has_more=False)
    func = _SEARCH_PAGE_FUNCS.get(resolved)
    if func is None:
        return SearchPage(candidates=[], page=page, has_more=False)
    return await func(keyword, page=page)


def store_search_keyword(location_label: str | None) -> str | None:
    """Store-search keyword for a location label.

    Labels may carry a trailing note to tell same-district spots apart
    (e.g. "동작구(상도)"); store search matches plain text, so only the part
    before the parenthesis is sent.
    """
    if location_label is None:
        return None
    return location_label.split("(", 1)[0].strip() or location_label.strip()


async def check_stock(
    brand: str | StoreBrand,
    product_code: str,
    *,
    store_keyword: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockLookup | None:
    """Run a real-time stock lookup for any brand. None if the brand has none.

    Brands take different location inputs (7-Eleven/Emart24 a store keyword,
    GS25/CU coordinates), so this is the single place that
    knows which arguments each one wants.
    """
    try:
        resolved = StoreBrand(brand)
    except ValueError:
        return None
    store_keyword = store_search_keyword(store_keyword)

    if resolved is StoreBrand.SEVEN_ELEVEN:
        return await check_seveneleven_stock(
            product_code,
            store_keyword=store_keyword,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
    if resolved is StoreBrand.EMART24:
        return await check_emart24_stock(
            product_code,
            store_keyword=store_keyword,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
    if resolved is StoreBrand.GS25:
        if latitude is not None and longitude is not None:
            return await check_gs25_stock(
                product_code,
                latitude=latitude,
                longitude=longitude,
                radius_meters=radius_meters,
            )
        return await check_gs25_stock(product_code)
    if resolved is StoreBrand.CU:
        return await check_cu_stock(
            product_code,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
    return None
