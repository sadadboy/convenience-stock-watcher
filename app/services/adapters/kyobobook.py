"""Kyobo Book (교보문고) store adapter.

Two public, unauthenticated endpoints back this adapter:

* **Product search** — the search results page at ``search.kyobobook.co.kr``.
  There is no JSON search API, so candidates are parsed out of the page's
  ``data-kbbfn-*`` image attributes, which carry the product id, ISBN and title
  in one tag.
* **Store stock** — ``/api/gw/pdt/product/{id}/location-inventory``, the API
  behind the product page's "매장 재고 · 위치" button. One call returns every
  Kyobo store nationwide with an exact per-store quantity, so unlike the
  convenience-store adapters there is no store-search step and no radius.

Both calls need a browser-like ``User-Agent``; without one the server answers
200 with an empty body. Every network call is best-effort: on any network/parse
error we log a warning and degrade gracefully.
"""

from __future__ import annotations

import logging
import re
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

_HEADERS = {
    # A browser-like UA is required; the server returns an empty body without it.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# Product ids on the thumbnail tag, e.g. data-kbbfn-pid="S000000610650".
_PID_RE = re.compile(r'data-kbbfn-pid="([SE]\d{6,})"')
_ATTR_RE = re.compile(r'data-kbbfn-([a-z]+)="([^"]*)"')
_PRICE_RE = re.compile(r'class="val">([\d,]+)</span>')

# ``data-kbbfn-type`` values that never carry store stock: ebooks, audiobooks,
# SAM subscriptions and print-on-demand. Verified to report 0 at all 37 stores,
# so they would only ever be dead watches.
_NON_PHYSICAL_TYPES = {"EBK", "AUD", "SAM", "POD"}


def _to_price(value: str) -> int | None:
    try:
        price = int(value.replace(",", ""))
    except (TypeError, ValueError):
        return None
    # Guard against picking up a discount badge ("10%") instead of the price.
    return price if price >= 100 else None


def _parse_candidates(html: str, limit: int) -> list[ProductCandidate]:
    """Parse candidates out of a search results page.

    The page has no per-product container we can rely on, so each product's
    segment is taken as the span between its ``data-kbbfn-pid`` anchor and the
    next one, and the price is read from the first ``.val`` inside that span.
    """
    anchors = [(m.group(1), m.start()) for m in _PID_RE.finditer(html)]
    candidates: list[ProductCandidate] = []
    seen: set[str] = set()

    for index, (pid, start) in enumerate(anchors):
        if pid in seen:
            continue
        end = anchors[index + 1][1] if index + 1 < len(anchors) else start + 4000
        segment = html[start:end]
        attrs = dict(_ATTR_RE.findall(segment))

        if attrs.get("type", "").upper() in _NON_PHYSICAL_TYPES:
            continue  # ebooks/SAM have no store inventory
        title = (attrs.get("title") or "").strip()
        if not title:
            continue

        price = None
        for raw in _PRICE_RE.findall(segment):
            price = _to_price(raw)
            if price is not None:
                break

        seen.add(pid)
        candidates.append(
            ProductCandidate(
                brand=StoreBrand.KYOBOBOOK,
                external_product_code=pid,
                external_product_name=title,
                barcode=(attrs.get("bid") or "").strip() or None,
                price=price,
            )
        )
        if len(candidates) >= limit:
            break

    return candidates


async def search_kyobobook_products(keyword: str, *, page: int = 1) -> SearchPage:
    """Search one page of Kyobo Book candidates by name. Never raises."""
    query = keyword.strip()
    page = max(page, 1)
    if not query:
        return SearchPage(candidates=[], page=page, has_more=False)
    if not settings.kyobobook_search_enabled:
        logger.info("Kyobo live search disabled; returning no candidates for %r", query)
        return SearchPage(candidates=[], page=page, has_more=False)

    url = settings.kyobobook_search_base_url.rstrip("/") + settings.kyobobook_search_path
    params = {"keyword": query, "page": str(page)}

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.get(url, headers=_HEADERS, params=params, follow_redirects=True)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError as exc:
        logger.warning("Kyobo product search network error for %r: %s", query, exc)
        return SearchPage(candidates=[], page=page, has_more=False)

    candidates = _parse_candidates(html, settings.kyobobook_search_limit)
    logger.info("Kyobo search %r page %d -> %d candidate(s)", query, page, len(candidates))
    # The page renders a fixed result count; a full page implies more may follow.
    has_more = len(candidates) >= settings.kyobobook_search_limit
    return SearchPage(candidates=candidates, page=page, has_more=has_more)


def _matches_keyword(store: dict, keyword: str) -> bool:
    if not keyword:
        return True
    haystack = f"{store.get('strName') or ''} {store.get('strAdrs') or ''}"
    return keyword in haystack


def _to_qty(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def check_kyobobook_stock(
    product_code: str,
    *,
    store_keyword: str | None = None,
) -> StockLookup:
    """Look up per-store stock for one Kyobo product id. Never raises.

    A single call returns every store nationwide, so ``store_keyword`` only
    narrows the result locally (matched against store name and address). An
    empty keyword keeps all stores.
    """
    code = product_code.strip()
    keyword = (store_keyword or settings.kyobobook_default_store_keyword).strip()
    now = datetime.now(timezone.utc)
    context = f"'{keyword}' 매장" if keyword else "전국 매장"

    def _result(*, ok: bool, stores: list[StoreStock] | None = None, error: str | None = None):
        return StockLookup(
            checked_at=now,
            brand=StoreBrand.KYOBOBOOK,
            item_code=code,
            ok=ok,
            stores=stores or [],
            error=error,
            context=context,
            # Every matching store comes back in one call, so scanned == stores.
            scanned_stores=stores or [],
        )

    if not code:
        return _result(ok=False, error="상품코드(saleCmdtId)가 비어 있습니다.")
    if not settings.kyobobook_stock_enabled:
        logger.info("Kyobo stock lookup disabled; skipping for %s", code)
        return _result(ok=False, error="교보문고 실시간 재고 조회가 비활성화되어 있습니다.")

    url = settings.kyobobook_product_base_url.rstrip("/") + settings.kyobobook_inventory_path.format(
        product_code=code
    )

    try:
        async with httpx.AsyncClient(timeout=settings.adapter_request_timeout) as client:
            response = await client.get(url, headers=_HEADERS)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Kyobo stock lookup network error for %s: %s", code, exc)
        return _result(ok=False, error="교보문고 재고 조회 네트워크 오류가 발생했습니다.")
    except ValueError as exc:
        logger.warning("Kyobo stock lookup returned invalid JSON for %s: %s", code, exc)
        return _result(ok=False, error="교보문고 재고 응답을 해석하지 못했습니다.")

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return _result(ok=False, error="교보문고 재고 응답 형식이 예상과 다릅니다.")

    # data is grouped by region (strAreaGrpCode); flatten it.
    results: list[StoreStock] = []
    for group in payload["data"]:
        if not isinstance(group, dict):
            continue
        for raw in group.get("list") or []:
            if not isinstance(raw, dict) or not _matches_keyword(raw, keyword):
                continue
            quantity = _to_qty(raw.get("realInvnQntt"))
            results.append(
                StoreStock(
                    store_code=str(raw.get("strRdpCode") or "").strip(),
                    store_name=str(raw.get("strName") or "").strip(),
                    address=str(raw.get("strAdrs") or "").strip(),
                    quantity=quantity,
                    in_stock=quantity > 0,
                )
            )

    if not results:
        where = f"'{keyword}'에 해당하는" if keyword else "조회 가능한"
        return _result(ok=False, error=f"{where} 교보문고 매장을 찾지 못했습니다.")

    results.sort(key=lambda store: store.quantity or 0, reverse=True)
    logger.info(
        "Kyobo stock lookup for %s (%s): %d store(s), %d in stock",
        code, context, len(results), sum(1 for s in results if s.in_stock),
    )
    return _result(ok=True, stores=results)


class KyoboBookAdapter:
    """Adapter implementing the Kyobo Book side of ``ConvenienceStoreAdapter``.

    Product search and real-time per-store stock are implemented; coordinate
    store search is not, since the inventory API exposes no store coordinates.
    """

    brand = StoreBrand.KYOBOBOOK

    async def search_products(self, keyword: str) -> list[ProductCandidate]:
        return (await search_kyobobook_products(keyword)).candidates

    async def search_stores(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[StoreCandidate]:
        raise NotImplementedError("Kyobo exposes no store coordinates for radius search.")

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
        result = await check_kyobobook_stock(product_code, store_keyword=store_keyword)
        stores = result.stores
        if store_code is not None:
            stores = [store for store in stores if store.store_code == store_code]
        quantity = sum(store.quantity or 0 for store in stores)
        return InventoryResult(
            in_stock=any(store.in_stock for store in stores),
            quantity=quantity,
            raw_payload={"ok": result.ok, "error": result.error, "context": result.context},
        )
