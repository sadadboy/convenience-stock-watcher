"""Convenience-store brand adapters and a small registry.

Each brand adapter exposes at least ``search_products``. Use ``search_products``
(single page) or ``search_products_page`` (paged) to run a brand-agnostic
candidate search from the API layer.
"""

from __future__ import annotations

from app.services.adapters.emart24 import Emart24Adapter, search_emart24_products
from app.services.adapters.gs25 import GS25Adapter, search_gs25_products
from app.services.adapters.seven_eleven import SevenElevenAdapter, search_seveneleven_products
from app.services.store_adapter import ProductCandidate, SearchPage, StoreBrand

# Brands with a working product-name search adapter.
_SEARCH_ADAPTERS = {
    StoreBrand.GS25: GS25Adapter(),
    StoreBrand.SEVEN_ELEVEN: SevenElevenAdapter(),
    StoreBrand.EMART24: Emart24Adapter(),
}

# Paged search functions per brand.
_SEARCH_PAGE_FUNCS = {
    StoreBrand.GS25: search_gs25_products,
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
