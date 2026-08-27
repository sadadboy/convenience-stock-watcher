"""Emart24 (이마트24) store adapter.

Product-name search against the public ``everse.emart24.co.kr`` stock-search
endpoint (no authentication required). Real-time per-store stock needs a store
business-number (bizNo) lookup flow and is deferred to a later milestone.

Every network call is best-effort: on any network/parse error we log a warning
and return an empty candidate list so the UI degrades gracefully.
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
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Linux; Android 15)",
}


def _to_price(*values: Any) -> int | None:
    """Return the first positive integer price among the given values."""
    for value in values:
        try:
            if value in (None, ""):
                continue
            price = int(float(str(value).strip()))
            if price > 0:
                return price
        except (TypeError, ValueError):
            continue
    return None


def _parse_candidates(payload: Any) -> list[ProductCandidate]:
    """Extract candidates from a stock/search response, defensively.

    Shape: ``{"productList": [{"pluCd", "goodsNm", "viewPrice"/"originPrice"/
    "salePrice", ...}]}``. Dedupe by ``pluCd``.
    """

    if not isinstance(payload, dict):
        return []

    products = payload.get("productList")
    if not isinstance(products, list):
        return []

    candidates: list[ProductCandidate] = []
    seen_codes: set[str] = set()

    for product in products:
        if not isinstance(product, dict):
            continue
        code = str(product.get("pluCd") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        name = str(product.get("goodsNm") or "").strip()
        price = _to_price(
            product.get("salePrice"),
            product.get("viewPrice"),
            product.get("originPrice"),
        )
        candidates.append(
            ProductCandidate(
                brand=StoreBrand.EMART24,
                external_product_code=code,
                external_product_name=name or code,
                barcode=code,
                price=price,
            )
        )

    return candidates


async def search_emart24_products(keyword: str, *, page: int = 1) -> SearchPage:
    """Search one page of Emart24 candidates by name. Never raises.

    The Emart24 API returns a fixed 10 items per page (ignores pageCnt), so more
    results are reached by paging via ``currentPage``.
    """

    query = keyword.strip()
    page = max(page, 1)
    if not query or not settings.emart24_search_enabled:
        if not query:
            return SearchPage(candidates=[], page=page, has_more=False)
        logger.info("Emart24 live search disabled; returning no candidates for %r", query)
        return SearchPage(candidates=[], page=page, has_more=False)

    url = settings.emart24_base_url.rstrip("/") + settings.emart24_search_path
    form = {
        "currentPage": str(page),
        "pageCnt": "10",
        "sortType": "",
        "saleProductYn": "",
        "searchWord": query,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.post(url, headers=_SEARCH_HEADERS, data=form)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Emart24 product search network error for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)
    except ValueError as exc:
        logger.warning("Emart24 product search returned invalid JSON for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)

    candidates = _parse_candidates(payload)
    # totalCnt is only returned on page 1, so infer "more" from a full page.
    raw = payload.get("productList") if isinstance(payload, dict) else None
    raw_count = len(raw) if isinstance(raw, list) else 0
    has_more = raw_count >= 10

    logger.info(
        "Emart24 search %r page %d -> %d candidate(s) (more=%s)",
        query, page, len(candidates), has_more,
    )
    return SearchPage(candidates=candidates, page=page, has_more=has_more)


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


async def _fetch_stores(client: httpx.AsyncClient, store_keyword: str, limit: int) -> dict[str, dict]:
    """Search Emart24 stores by keyword. Returns {bizNo(CODE): {name, address}}."""
    url = settings.emart24_web_base_url.rstrip("/") + settings.emart24_store_search_path
    response = await client.get(
        url,
        headers=_SEARCH_HEADERS,
        params={"page": "1", "search": store_keyword},
    )
    response.raise_for_status()
    payload = response.json()

    stores: dict[str, dict] = {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("CODE") or "").strip()
        if not code or code in stores:
            continue
        if len(stores) >= limit:
            break
        stores[code] = {
            "name": str(row.get("TITLE") or "").strip(),
            "address": str(row.get("ADDRESS") or "").strip(),
            "latitude": _to_float(row.get("LATITUDE")),
            "longitude": _to_float(row.get("LONGITUDE")),
        }
    return stores


async def _fetch_stock_qty(client: httpx.AsyncClient, plu_code: str, biz_nos: list[str]) -> dict[str, int]:
    """Fetch per-store quantities. Returns {bizNo: quantity}."""
    url = settings.emart24_base_url.rstrip("/") + settings.emart24_stock_search_path
    response = await client.get(
        url,
        headers=_SEARCH_HEADERS,
        params={"searchPluCode": plu_code, "bizNoArr": ",".join(biz_nos)},
    )
    response.raise_for_status()
    payload = response.json()

    quantities: dict[str, int] = {}
    rows = payload.get("storeGoodsQty") if isinstance(payload, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        biz_no = str(row.get("BIZNO") or "").strip()
        if biz_no:
            quantities[biz_no] = _to_int(row.get("BIZQTY"))
    return quantities


async def check_emart24_stock(
    item_code: str,
    *,
    store_keyword: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockLookup:
    """Look up real-time Emart24 stock for a pluCd near a store keyword.

    Multi-step: store search (bizNo) -> stock-search by bizNoArr. When
    lat/lon/radius are given, stores are filtered to within the radius of the
    point (the keyword still seeds the fetch). Never raises.
    """

    code = item_code.strip()
    keyword = (store_keyword or settings.emart24_default_store_keyword).strip()
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
            brand=StoreBrand.EMART24,
            item_code=code,
            ok=ok,
            stores=stores or [],
            error=error,
            context=context,
            scanned_stores=scanned or [],
        )

    if not code:
        return _result(ok=False, error="상품코드(pluCd)가 비어 있습니다.")
    if not keyword:
        return _result(ok=False, error="매장 검색 키워드가 필요합니다 (예: 강남).")
    if not settings.emart24_stock_enabled:
        logger.info("Emart24 stock lookup disabled; skipping for pluCd %s", code)
        return _result(ok=False, error="Emart24 실시간 재고 조회가 비활성화되어 있습니다.")

    fetch_limit = 200 if use_radius else settings.emart24_store_limit

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            stores = await _fetch_stores(client, keyword, fetch_limit)
            if use_radius:
                stores = _filter_by_radius(stores, latitude, longitude, radius_meters)
            if not stores:
                where = "반경 내" if use_radius else f"'{keyword}'"
                return _result(ok=False, error=f"{where} 매장을 찾지 못했습니다.")

            quantities = await _fetch_stock_qty(client, code, list(stores.keys()))
    except httpx.HTTPError as exc:
        logger.warning("Emart24 stock lookup network error for pluCd %s: %s", code, exc)
        return _result(ok=False, error="Emart24 재고 조회 네트워크 오류가 발생했습니다.")
    except ValueError as exc:
        logger.warning("Emart24 stock lookup returned invalid JSON for pluCd %s: %s", code, exc)
        return _result(ok=False, error="Emart24 재고 응답을 해석하지 못했습니다.")

    # Stores missing from the response don't carry the SKU (the API does return
    # BIZQTY=0 rows), so keep them in ``scanned`` with quantity=None.
    results: list[StoreStock] = []
    scanned: list[StoreStock] = []
    for biz_no, info in stores.items():
        quantity = quantities.get(biz_no)
        store = StoreStock(
            store_code=biz_no,
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
        "Emart24 stock lookup for pluCd %s near %r: %d/%d store(s) with data, %d in stock",
        code,
        keyword,
        len(results),
        len(scanned),
        sum(1 for s in results if s.in_stock),
    )
    return _result(ok=True, stores=results, scanned=scanned)


class Emart24Adapter:
    """Adapter implementing the Emart24 side of ``ConvenienceStoreAdapter``.

    Product search and real-time stock (via a store keyword) are implemented;
    coordinate-based store search is deferred.
    """

    brand = StoreBrand.EMART24

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        return (await search_emart24_products(keyword)).candidates

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        raise NotImplementedError("Emart24 coordinate store search is not implemented yet.")

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
        result = await check_emart24_stock(
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
