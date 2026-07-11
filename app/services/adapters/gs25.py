"""GS25 store adapter.

For now this only implements product-name search against the unofficial
woodongs total-search endpoint. The flow (mirrored from the reference project
``hmmhmmhm/daiso-mcp``) is:

1. Search by product name -> get ``itemCode`` candidates.
2. (Later step) Use ``itemCode`` + coordinates to look up per-store stock.

Only step 1 is implemented here. Every network call is best-effort: on any
network/parse error we log a warning and return an empty candidate list so the
UI degrades gracefully instead of raising.
"""

from __future__ import annotations

import logging
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
)

logger = logging.getLogger(__name__)

# The endpoint is served for the woodongs mobile web/app, so we mimic those
# origin headers. Nothing here is authenticated.
_SEARCH_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": "https://woodongs.com",
    "Referer": "https://woodongs.com/",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15; SM-S928N) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 Chrome/124.0 Mobile Safari/537.36"
    ),
}


def _parse_candidates(payload: Any) -> list[ProductCandidate]:
    """Extract product candidates from a total-search response, defensively.

    The response shape is::

        SearchQueryResult.Collection[].Documentset.Document[].field

    where ``field`` holds ``itemCode``, ``itemName``, ``itemImageUrl`` etc.
    Any missing/None node is treated as empty so a partial payload never raises.
    """

    if not isinstance(payload, dict):
        return []

    result = payload.get("SearchQueryResult") or {}
    collections = result.get("Collection") or []

    candidates: list[ProductCandidate] = []
    seen_codes: set[str] = set()

    for collection in collections:
        if not isinstance(collection, dict):
            continue
        document_set = collection.get("Documentset") or {}
        documents = document_set.get("Document") or []
        for document in documents:
            if not isinstance(document, dict):
                continue
            field = document.get("field") or {}
            item_code = str(field.get("itemCode") or "").strip()
            if not item_code or item_code in seen_codes:
                continue
            seen_codes.add(item_code)

            name = str(field.get("itemName") or field.get("shortItemName") or "").strip()
            image_url = str(field.get("itemImageUrl") or "").strip() or None

            candidates.append(
                ProductCandidate(
                    brand=StoreBrand.GS25,
                    external_product_code=item_code,
                    external_product_name=name or item_code,
                    image_url=image_url,
                    # GS25 search responses do not include price; left unset.
                    price=None,
                )
            )

    return candidates


async def search_gs25_products(keyword: str, *, page: int = 1) -> SearchPage:
    """Search GS25 candidates by name. Never raises.

    The woodongs total-search returns all matches in one response, so page 1
    holds everything and ``has_more`` is always False.
    """

    query = keyword.strip()
    page = max(page, 1)
    if not query or not settings.gs25_search_enabled:
        if query:
            logger.info("GS25 live search disabled; returning no candidates for %r", query)
        return SearchPage(candidates=[], page=page, has_more=False)

    if page > 1:
        return SearchPage(candidates=[], page=page, has_more=False)

    url = settings.gs25_api_base_url.rstrip("/") + settings.gs25_search_path

    try:
        async with httpx.AsyncClient(timeout=settings.gs25_request_timeout) as client:
            response = await client.post(url, headers=_SEARCH_HEADERS, json={"query": query})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("GS25 product search network error for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)
    except ValueError as exc:  # invalid / non-JSON body
        logger.warning("GS25 product search returned invalid JSON for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)

    candidates = _parse_candidates(payload)
    logger.info("GS25 search %r -> %d candidate(s)", query, len(candidates))
    return SearchPage(candidates=candidates, page=1, has_more=False)


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _parse_stock_stores(payload: Any) -> list[StoreStock]:
    if not isinstance(payload, dict):
        return []

    raw_stores = payload.get("stores")
    if not isinstance(raw_stores, list):
        return []

    stores: list[StoreStock] = []
    for raw in raw_stores:
        if not isinstance(raw, dict):
            continue
        store_code = str(raw.get("storeCode") or "").strip()
        if not store_code:
            continue
        quantity = _to_int(raw.get("realStockQuantity"))
        sold_out = str(raw.get("isSoldOutYn") or "").strip().upper() == "Y"
        price_raw = raw.get("searchItemSellPrice")
        price = _to_int(price_raw) if price_raw not in (None, "") else None
        stores.append(
            StoreStock(
                store_code=store_code,
                store_name=str(raw.get("storeName") or "").strip(),
                address=str(raw.get("storeAddress") or "").strip(),
                quantity=quantity,
                in_stock=(quantity > 0 and not sold_out),
                price=price,
            )
        )
    return stores


async def check_gs25_stock(
    item_code: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockLookup:
    """Look up real-time GS25 store stock for an itemCode. Never raises.

    On disabled config or any network/parse failure it returns a result with
    ``ok=False`` and an ``error`` message (all logged), so callers can render a
    graceful message instead of crashing.
    """

    code = item_code.strip()
    lat = latitude if latitude is not None else settings.gs25_default_latitude
    lon = longitude if longitude is not None else settings.gs25_default_longitude
    radius = radius_meters if radius_meters is not None else settings.gs25_default_radius_meters
    now = datetime.now(timezone.utc)
    context = f"좌표 ({lat}, {lon}) · 반경 {radius}m"

    def _result(*, ok: bool, stores: list[StoreStock] | None = None, error: str | None = None):
        return StockLookup(
            checked_at=now,
            brand=StoreBrand.GS25,
            item_code=code,
            ok=ok,
            stores=stores or [],
            error=error,
            context=context,
        )

    if not code:
        return _result(ok=False, error="itemCode가 비어 있습니다.")

    if not settings.gs25_stock_enabled:
        logger.info("GS25 stock lookup disabled; skipping for itemCode %s", code)
        return _result(ok=False, error="GS25 실시간 재고 조회가 비활성화되어 있습니다.")

    url = settings.gs25_bff_base_url.rstrip("/") + settings.gs25_stock_path
    # latitude -> Y coordinate, longitude -> X coordinate (per woodongs app).
    params = {
        "serviceCode": "01",
        "realTimeStockYn": "Y",
        "itemCode": code,
        "myPositionYCoordination": str(lat),
        "myPositionXCoordination": str(lon),
        "centerPositionYCoordination": str(lat),
        "centerPositionXCoordination": str(lon),
        "radiusCondition": str(radius),
        "pickupStoreYn": "N",
        "isSuperDlvyStoreSelected": "N",
        "isGs25DlvyStoreSelected": "N",
    }

    headers = dict(_SEARCH_HEADERS)
    token = settings.gs25_auth_token.strip()
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=settings.gs25_request_timeout) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            logger.warning(
                "GS25 stock lookup unauthorized (%s) for itemCode %s; "
                "a valid GS25_AUTH_TOKEN (woodongs app token) is likely required.",
                exc.response.status_code,
                code,
            )
            return _result(
                ok=False,
                error="GS25 실시간 재고 API 인증이 필요합니다 (GS25_AUTH_TOKEN 미설정/만료).",
            )
        logger.warning("GS25 stock lookup HTTP error for itemCode %s: %s", code, exc)
        return _result(ok=False, error="GS25 재고 조회 중 오류가 발생했습니다.")
    except httpx.HTTPError as exc:
        logger.warning("GS25 stock lookup network error for itemCode %s: %s", code, exc)
        return _result(ok=False, error="GS25 재고 조회 네트워크 오류가 발생했습니다.")
    except ValueError as exc:
        logger.warning("GS25 stock lookup returned invalid JSON for itemCode %s: %s", code, exc)
        return _result(ok=False, error="GS25 재고 응답을 해석하지 못했습니다.")

    stores = _parse_stock_stores(payload)
    logger.info(
        "GS25 stock lookup for itemCode %s returned %d store(s), %d in stock",
        code,
        len(stores),
        sum(1 for s in stores if s.in_stock),
    )
    return _result(ok=True, stores=stores)


class GS25Adapter:
    """Adapter implementing the GS25 side of :class:`ConvenienceStoreAdapter`.

    Only ``search_products`` is wired up for now. Store search and inventory
    lookup are intentionally deferred to a later milestone.
    """

    brand = StoreBrand.GS25

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        return (await search_gs25_products(keyword)).candidates

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        raise NotImplementedError("GS25 store search is not implemented yet.")

    async def check_inventory(
        self,
        *,
        product_code: str,
        store_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
    ) -> InventoryResult:
        result = await check_gs25_stock(
            product_code,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
        stores = result.stores
        if store_code is not None:
            stores = [store for store in stores if store.store_code == store_code]

        in_stock = any(store.in_stock for store in stores)
        quantity = sum(store.quantity or 0 for store in stores)
        return InventoryResult(
            in_stock=in_stock,
            quantity=quantity if stores else None,
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
                        "price": store.price,
                    }
                    for store in stores
                ],
            },
        )
