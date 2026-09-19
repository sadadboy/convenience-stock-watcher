"""7-Eleven (세븐일레븐) store adapter.

Product-name search against the public ``/api/v1/open/search/goods`` endpoint
(no authentication required). Real-time per-store stock is a multi-step flow
(itemCd -> smCd/stokMngCd -> store list -> real-stock) and is intentionally
deferred to a later milestone.

Like the GS25 adapter, every network call is best-effort: on any network/parse
error we log a warning and return an empty candidate list so the UI degrades
gracefully instead of raising.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.store_adapter import (
    InventoryResult,
    ProductCandidate,
    SearchPage,
    StockLookup,
    StoreBrand,
    StoreCandidate,
    StoreStock,
    haversine_m,
    scanned_sort_key,
)

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _filter_by_radius(stores: dict, latitude, longitude, radius_meters) -> dict:
    """Keep only stores within radius_meters of the point (if coords given)."""
    if latitude is None or longitude is None or not radius_meters:
        return stores
    kept = {}
    for code, info in stores.items():
        slat, slon = info.get("latitude"), info.get("longitude")
        if slat is None or slon is None:
            continue
        if haversine_m(latitude, longitude, slat, slon) <= radius_meters:
            kept[code] = info
    return kept

_SEARCH_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Linux; Android 15)",
}


def _to_price(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        price = int(float(str(value).strip()))
        return price if price > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_candidates(payload: Any) -> list[ProductCandidate]:
    """Extract candidates from a search/goods response, defensively.

    Shape: ``data.SearchQueryResult.Collection[].Documentset.Document[].field``
    with ``itemCd`` (barcode/code), ``itemOnm`` (name), ``itemPrice``. The same
    item can appear across collections (pickup/offline/...), so we dedupe by
    ``itemCd``.
    """

    if not isinstance(payload, dict):
        return []

    data = payload.get("data") or {}
    result = data.get("SearchQueryResult") or {}
    collections = result.get("Collection") or []

    candidates: list[ProductCandidate] = []
    seen_codes: set[str] = set()

    # In-store ("offline") items first: those are what stock lookups can find.
    collections = sorted(
        (c for c in collections if isinstance(c, dict)),
        key=lambda c: c.get("CollectionId") != "offline",
    )
    for collection in collections:
        document_set = collection.get("Documentset") or {}
        documents = document_set.get("Document") or []
        for document in documents:
            if not isinstance(document, dict):
                continue
            field = document.get("field") or {}
            item_code = str(field.get("itemCd") or "").strip()
            if not item_code or item_code in seen_codes:
                continue
            seen_codes.add(item_code)

            name = str(field.get("itemOnm") or "").strip()
            candidates.append(
                ProductCandidate(
                    brand=StoreBrand.SEVEN_ELEVEN,
                    external_product_code=item_code,
                    external_product_name=name or item_code,
                    barcode=item_code,
                    price=_to_price(field.get("itemPrice")),
                )
            )

    return candidates


# The search engine ignores offsets past ~40 (returns nothing) but happily
# returns every match in one call, so we fetch everything once and serve pages
# from a short-lived cache while the user scrolls.
_FULL_FETCH_COUNT = 1000
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 32
_search_cache: dict[str, tuple[float, list[ProductCandidate]]] = {}


async def _fetch_all_candidates(query: str) -> list[ProductCandidate] | None:
    """All candidates for a query (cached). None on network/parse failure."""
    now = time.monotonic()
    cached = _search_cache.get(query)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    url = settings.seveneleven_base_url.rstrip("/") + settings.seveneleven_search_path
    body = {"query": query, "startCount": 0, "listCount": _FULL_FETCH_COUNT}
    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.post(url, headers=_SEARCH_HEADERS, json=body)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("7-Eleven product search network error for %r: %s", query, exc)
        return None
    except ValueError as exc:
        logger.warning("7-Eleven product search returned invalid JSON for %r: %s", query, exc)
        return None

    candidates = _parse_candidates(payload)
    if len(_search_cache) >= _CACHE_MAX_ENTRIES:
        _search_cache.pop(min(_search_cache, key=lambda key: _search_cache[key][0]))
    _search_cache[query] = (now, candidates)
    return candidates


async def search_seveneleven_products(keyword: str, *, page: int = 1) -> SearchPage:
    """Search one page of 7-Eleven candidates by name. Never raises.

    ``pageNo``/``pageSize`` are ignored upstream (each collection is capped at
    10) and ``startCount`` stops working past ~40, so all matches are fetched
    in one call and paged locally.
    """

    query = keyword.strip()
    page = max(page, 1)
    if not query or not settings.seveneleven_search_enabled:
        if query:
            logger.info("7-Eleven live search disabled; returning no candidates for %r", query)
        return SearchPage(candidates=[], page=page, has_more=False)

    candidates = await _fetch_all_candidates(query)
    if candidates is None:
        return SearchPage(candidates=[], page=page, has_more=False)

    limit = settings.seveneleven_search_limit
    start = (page - 1) * limit
    page_items = candidates[start : start + limit]
    has_more = start + limit < len(candidates)
    logger.info(
        "7-Eleven search %r page %d -> %d of %d candidate(s) (more=%s)",
        query, page, len(page_items), len(candidates), has_more,
    )
    return SearchPage(candidates=page_items, page=page, has_more=has_more)


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


async def _fetch_product_meta(client: httpx.AsyncClient, item_code: str) -> dict | None:
    """Fetch smCd / stokMngCd / stokMngQty / stockApplicationRate for an itemCd."""
    url = settings.seveneleven_base_url.rstrip("/") + settings.seveneleven_product_meta_path
    response = await client.get(url, headers=_SEARCH_HEADERS, params={"itemCd": item_code})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    sm_code = str(payload.get("smCd") or "").strip()
    if not sm_code:
        return None
    return {
        "smCd": sm_code,
        "stokMngCd": str(payload.get("stokMngCd") or "").strip(),
        "stokMngQty": _to_int(payload.get("stokMngQty")),
        "stockApplicationRate": str(payload.get("stockApplicationRate") or ""),
    }


async def _fetch_stores(client: httpx.AsyncClient, store_keyword: str, limit: int) -> dict[str, dict]:
    """Search stores by keyword. Returns {storeCd: {name, address}}."""
    url = settings.seveneleven_base_url.rstrip("/") + settings.seveneleven_store_search_path
    body = {"collection": "store", "query": store_keyword, "sort": "Date/desc", "listCount": limit}
    response = await client.post(url, headers=_SEARCH_HEADERS, json=body)
    response.raise_for_status()
    payload = response.json()

    stores: dict[str, dict] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    result = (data or {}).get("SearchQueryResult") or {}
    for collection in result.get("Collection") or []:
        if not isinstance(collection, dict):
            continue
        for document in (collection.get("Documentset") or {}).get("Document") or []:
            if not isinstance(document, dict):
                continue
            field = document.get("field") or {}
            code = str(field.get("storeCd") or field.get("storeCode") or "").strip()
            if not code or code in stores:
                continue
            addr = " ".join(
                part
                for part in (str(field.get("addr1") or "").strip(), str(field.get("addr2") or "").strip())
                if part
            )
            stores[code] = {
                "name": str(field.get("storeNm") or "").strip(),
                "address": addr,
                "latitude": _to_float(field.get("storeLat")),
                "longitude": _to_float(field.get("storeLon")),
            }
    return stores


async def _fetch_real_stock(client: httpx.AsyncClient, meta: dict, store_codes: list[str]) -> dict[str, int]:
    """POST real-stock for the given stores. Returns {storeCd: quantity}."""
    url = settings.seveneleven_base_url.rstrip("/") + settings.seveneleven_real_stock_path
    payload = {
        "smCd": meta["smCd"],
        "stokMngCd": meta["stokMngCd"],
        "stokMngQty": meta["stokMngQty"],
        "stockApplicationRate": meta["stockApplicationRate"],
        "storeList": store_codes,
    }
    response = await client.post(url, headers=_SEARCH_HEADERS, json=payload)
    response.raise_for_status()
    body = response.json()

    quantities: dict[str, int] = {}
    data = body.get("data") if isinstance(body, dict) else None
    for row in (data or {}).get("storeList") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("storeCd") or "").strip()
        if code:
            quantities[code] = _to_int(row.get("stock"))
    return quantities


async def check_seveneleven_stock(
    item_code: str,
    *,
    store_keyword: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockLookup:
    """Look up real-time 7-Eleven stock for an itemCd near a store keyword.

    Multi-step: product meta (smCd) -> store search -> real-stock. When
    lat/lon/radius are given, stores are filtered to within the radius of the
    point (the keyword still seeds the fetch). Never raises.
    """

    code = item_code.strip()
    keyword = (store_keyword or settings.seveneleven_default_store_keyword).strip()
    use_radius = latitude is not None and longitude is not None and radius_meters
    now = datetime.now(timezone.utc)
    context = (
        f"'{keyword}' 반경 {radius_meters}m" if use_radius else f"매장 검색 '{keyword}'"
    )

    def _result(
        *,
        ok: bool,
        stores: list[StoreStock] | None = None,
        error: str | None = None,
        scanned: list[StoreStock] | None = None,
    ):
        return StockLookup(
            checked_at=now,
            brand=StoreBrand.SEVEN_ELEVEN,
            item_code=code,
            ok=ok,
            stores=stores or [],
            error=error,
            context=context,
            scanned_stores=scanned or [],
        )

    if not code:
        return _result(ok=False, error="상품코드(itemCd)가 비어 있습니다.")
    if not keyword:
        return _result(ok=False, error="매장 검색 키워드가 필요합니다 (예: 강남).")
    if not settings.seveneleven_stock_enabled:
        logger.info("7-Eleven stock lookup disabled; skipping for itemCd %s", code)
        return _result(ok=False, error="7-Eleven 실시간 재고 조회가 비활성화되어 있습니다.")

    # When filtering by radius, pull a wider store set to filter down from.
    fetch_limit = 200 if use_radius else settings.seveneleven_store_limit

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            meta = await _fetch_product_meta(client, code)
            if meta is None:
                return _result(ok=False, error="7-Eleven 상품 재고 메타(smCd)를 찾지 못했습니다.")

            stores = await _fetch_stores(client, keyword, fetch_limit)
            if use_radius:
                stores = _filter_by_radius(stores, latitude, longitude, radius_meters)
            if not stores:
                where = "반경 내" if use_radius else f"'{keyword}'"
                return _result(ok=False, error=f"{where} 매장을 찾지 못했습니다.")

            quantities = await _fetch_real_stock(client, meta, list(stores.keys()))
    except httpx.HTTPError as exc:
        logger.warning("7-Eleven stock lookup network error for itemCd %s: %s", code, exc)
        return _result(ok=False, error="7-Eleven 재고 조회 네트워크 오류가 발생했습니다.")
    except ValueError as exc:
        logger.warning("7-Eleven stock lookup returned invalid JSON for itemCd %s: %s", code, exc)
        return _result(ok=False, error="7-Eleven 재고 응답을 해석하지 못했습니다.")

    # Stores missing from the real-stock response don't carry the SKU (the API
    # does return stock=0 rows), so keep them in ``scanned`` with quantity=None.
    results: list[StoreStock] = []
    scanned: list[StoreStock] = []
    for store_code, info in stores.items():
        quantity = quantities.get(store_code)
        store = StoreStock(
            store_code=store_code,
            store_name=info["name"],
            address=info["address"],
            quantity=quantity,
            in_stock=quantity is not None and quantity > 0,
        )
        scanned.append(store)
        if quantity is not None:
            results.append(store)
    results.sort(key=lambda store: store.quantity or 0, reverse=True)
    scanned.sort(key=scanned_sort_key)

    logger.info(
        "7-Eleven stock lookup for itemCd %s near %r: %d/%d store(s) with data, %d in stock",
        code,
        keyword,
        len(results),
        len(scanned),
        sum(1 for s in results if s.in_stock),
    )
    return _result(ok=True, stores=results, scanned=scanned)


class SevenElevenAdapter:
    """Adapter implementing the 7-Eleven side of ``ConvenienceStoreAdapter``.

    Product search and real-time stock (via a store keyword) are implemented;
    coordinate-based store search is deferred.
    """

    brand = StoreBrand.SEVEN_ELEVEN

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        return (await search_seveneleven_products(keyword)).candidates

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        raise NotImplementedError("7-Eleven coordinate store search is not implemented yet.")

    async def check_inventory(
        self,
        *,
        product_code: str,
        store_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
        store_keyword: str | None = None,
    ) -> InventoryResult:
        result = await check_seveneleven_stock(
            product_code,
            store_keyword=store_keyword,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
        stores = result.stores
        if store_code is not None:
            stores = [store for store in stores if store.store_code == store_code]

        return InventoryResult(
            in_stock=any(store.in_stock for store in stores),
            quantity=sum(store.quantity or 0 for store in stores) if stores else None,
            raw_payload={
                "ok": result.ok,
                "error": result.error,
                "item_code": result.item_code,
                "context": result.context,
                "stores": [
                    {
                        "store_code": store.store_code,
                        "store_name": store.store_name,
                        "address": store.address,
                        "quantity": store.quantity,
                        "in_stock": store.in_stock,
                    }
                    for store in stores
                ],
            },
        )
