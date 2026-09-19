"""CU (포켓CU) store adapter.

The PocketCU app is a WebView over ``www.pocketcu.co.kr``, so both endpoints
are plain public web APIs (no login, session or token). The only gate is a WAF
rule that requires ``BGFCU`` somewhere in the User-Agent; we send the exact UA
the iOS app sends.

1. Product search: ``POST /api/search/rest/stock/main`` -> ``item_cd``.
2. Store search + stock in one call: ``POST /api/store`` with coordinates and
   ``jipCd=item_cd`` -> nearby stores (~1km, nearest first) with ``stock``.

Unlike 7-Eleven/Emart24 the store lookup is coordinate-based, so stock checks
need a map location (lat/lon), not a store keyword.

Every network call is best-effort: on any network/parse error we log a warning
and return an empty/failed result so the UI degrades gracefully.
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

# The server only checks for "BGFCU" in the UA; keep the real app value so a
# stricter WAF rule later is less likely to break us.
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148;BGFCU"
    ),
}

# Without these three fields /api/store still answers 200 but ``stock`` is null.
_STOCK_FIXED_FIELDS = {
    "recommendId": "stock",
    "pageType": "search_improve stock_sch_improve",
}


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_price(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        price = int(float(str(value).strip()))
        return price if price > 0 else None
    except (TypeError, ValueError):
        return None


def _to_quantity(value: Any) -> int | None:
    """``stock`` is a numeric string; null means no stock data for that store."""
    try:
        if value in (None, ""):
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _search_rows(payload: Any) -> tuple[list, int]:
    """Return (rows, total_count) from ``data.stockResult.result``."""
    if not isinstance(payload, dict):
        return [], 0
    data = payload.get("data") or {}
    stock_result = data.get("stockResult") if isinstance(data, dict) else None
    result = stock_result.get("result") if isinstance(stock_result, dict) else None
    if not isinstance(result, dict):
        return [], 0
    rows = result.get("rows")
    total = _to_quantity(result.get("total_count")) or 0
    return (rows if isinstance(rows, list) else []), total


def _parse_candidates(rows: list) -> list[ProductCandidate]:
    """Extract candidates from search rows (``rows[].fields``), deduped by item_cd."""
    candidates: list[ProductCandidate] = []
    seen_codes: set[str] = set()

    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            continue
        code = str(fields.get("item_cd") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        name = str(fields.get("item_nm") or "").strip()
        candidates.append(
            ProductCandidate(
                brand=StoreBrand.CU,
                external_product_code=code,
                external_product_name=name or code,
                barcode=code,
                image_url=str(fields.get("banner_img") or "").strip() or None,
                price=_to_price(fields.get("hyun_maega")),
            )
        )

    return candidates


async def search_cu_products(keyword: str, *, page: int = 1) -> SearchPage:
    """Search one page of CU candidates by name. Never raises."""

    query = keyword.strip()
    page = max(page, 1)
    if not query or not settings.cu_search_enabled:
        if query:
            logger.info("CU live search disabled; returning no candidates for %r", query)
        return SearchPage(candidates=[], page=page, has_more=False)

    limit = settings.cu_search_limit
    offset = (page - 1) * limit
    url = settings.cu_base_url.rstrip("/") + settings.cu_search_path
    body = {"searchWord": query, "offset": offset, "limit": limit, "searchSort": "recom"}

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.post(url, headers=_HEADERS, json=body)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("CU product search network error for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)
    except ValueError as exc:
        logger.warning("CU product search returned invalid JSON for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)

    rows, total = _search_rows(payload)
    candidates = _parse_candidates(rows)
    has_more = bool(rows) and offset + len(rows) < total

    logger.info(
        "CU search %r page %d -> %d candidate(s) (more=%s)",
        query, page, len(candidates), has_more,
    )
    return SearchPage(candidates=candidates, page=page, has_more=has_more)


def _store_distance(raw: dict, latitude: float, longitude: float) -> float | None:
    """Distance in meters: the API's ``distance`` field, else haversine."""
    distance = _to_float(raw.get("distance"))
    if distance is not None:
        return distance
    slat, slon = _to_float(raw.get("latVal")), _to_float(raw.get("longVal"))
    if slat is None or slon is None:
        return None
    return haversine_m(latitude, longitude, slat, slon)


async def _fetch_stores(
    client: httpx.AsyncClient,
    item_code: str,
    latitude: float,
    longitude: float,
) -> list[dict]:
    """Nearby stores with ``stock`` for item_code (nearest first)."""
    url = settings.cu_base_url.rstrip("/") + settings.cu_store_path
    body = {
        "latVal": str(latitude),
        "longVal": str(longitude),
        "jipCd": item_code,
        **_STOCK_FIXED_FIELDS,
    }
    response = await client.post(url, headers=_HEADERS, json=body)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("storeList") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


async def check_cu_stock(
    item_code: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_meters: int | None = None,
) -> StockLookup:
    """Look up real-time CU stock for an item_cd around a point. Never raises.

    The API itself returns stores within roughly 1km, so the radius is capped at
    ``cu_max_radius_meters``; a smaller radius drops stores farther than that.
    """

    code = item_code.strip()
    now = datetime.now(timezone.utc)
    has_coords = latitude is not None and longitude is not None
    max_radius = settings.cu_max_radius_meters
    radius = min(radius_meters, max_radius) if radius_meters else max_radius
    context = f"좌표 ({latitude}, {longitude}) · 반경 {radius}m" if has_coords else "위치 미지정"
    if has_coords and radius_meters and radius_meters > max_radius:
        context += f" (CU 최대, 요청 {radius_meters}m)"

    def _result(
        *,
        ok: bool,
        stores: list[StoreStock] | None = None,
        error: str | None = None,
        scanned: list[StoreStock] | None = None,
    ):
        return StockLookup(
            checked_at=now,
            brand=StoreBrand.CU,
            item_code=code,
            ok=ok,
            stores=stores or [],
            error=error,
            context=context,
            scanned_stores=scanned or [],
        )

    if not code:
        return _result(ok=False, error="상품코드(item_cd)가 비어 있습니다.")
    if not has_coords:
        return _result(
            ok=False,
            error="CU 재고 조회는 좌표 기반입니다. 지도에서 위치를 지정해 주세요.",
        )
    if not settings.cu_stock_enabled:
        logger.info("CU stock lookup disabled; skipping for item_cd %s", code)
        return _result(ok=False, error="CU 실시간 재고 조회가 비활성화되어 있습니다.")

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            rows = await _fetch_stores(client, code, latitude, longitude)
    except httpx.HTTPError as exc:
        logger.warning("CU stock lookup network error for item_cd %s: %s", code, exc)
        return _result(ok=False, error="CU 재고 조회 네트워크 오류가 발생했습니다.")
    except ValueError as exc:
        logger.warning("CU stock lookup returned invalid JSON for item_cd %s: %s", code, exc)
        return _result(ok=False, error="CU 재고 응답을 해석하지 못했습니다.")

    results: list[StoreStock] = []
    scanned: list[StoreStock] = []
    seen: set[str] = set()
    for raw in rows:
        store_code = str(raw.get("storeCd") or "").strip()
        if not store_code or store_code in seen:
            continue
        distance = _store_distance(raw, latitude, longitude)
        if distance is None or distance > radius:
            continue
        seen.add(store_code)

        address = " ".join(
            part
            for part in (
                str(raw.get("addrFst") or "").strip(),
                str(raw.get("addrDetail") or "").strip(),
            )
            if part
        )
        quantity = _to_quantity(raw.get("stock"))
        store = StoreStock(
            store_code=store_code,
            store_name=str(raw.get("storeNm") or "").strip(),
            address=address,
            quantity=quantity,
            in_stock=quantity is not None and quantity > 0,
        )
        scanned.append(store)
        if quantity is not None:
            results.append(store)

    if not scanned:
        return _result(ok=False, error="반경 내 CU 매장을 찾지 못했습니다.")

    results.sort(key=lambda store: store.quantity or 0, reverse=True)
    scanned.sort(key=scanned_sort_key)

    logger.info(
        "CU stock lookup for item_cd %s at (%s, %s): %d/%d store(s) with data, %d in stock",
        code,
        latitude,
        longitude,
        len(results),
        len(scanned),
        sum(1 for s in results if s.in_stock),
    )
    return _result(ok=True, stores=results, scanned=scanned)


class CUAdapter:
    """Adapter implementing the CU side of ``ConvenienceStoreAdapter``.

    Product search and coordinate-based real-time stock are implemented; a
    stock-less store search is not needed (the stock call returns stores).
    """

    brand = StoreBrand.CU

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        return (await search_cu_products(keyword)).candidates

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        raise NotImplementedError("CU standalone store search is not implemented yet.")

    async def check_inventory(
        self,
        *,
        product_code: str,
        store_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
    ) -> InventoryResult:
        result = await check_cu_stock(
            product_code,
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
