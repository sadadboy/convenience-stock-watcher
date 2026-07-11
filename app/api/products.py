import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.products import (
    create_product,
    create_product_source,
    delete_product,
    delete_product_source,
    get_product,
    list_products,
    set_product_enabled,
    set_product_source_enabled,
)
from app.repositories.location import (
    get_search_location,
    set_search_location_coords,
    set_search_location_keyword,
)
from app.schemas.product import ProductCreate, ProductSourceCreate
from app.core.config import settings
from app.services.adapters import SEARCH_BRANDS, search_products, search_products_page
from app.repositories.watches import create_watches_for_product
from app.services.geocoding import reverse_geocode_keyword
from app.services.adapters.emart24 import check_emart24_stock
from app.services.adapters.gs25 import check_gs25_stock
from app.services.adapters.seven_eleven import check_seveneleven_stock
from app.services.mock_inventory import check_mock_inventory
from app.services.store_adapter import StoreBrand

router = APIRouter(prefix="/products", tags=["products"])
templates = Jinja2Templates(directory="app/templates")

# Brands whose saved sources show a real-time stock button (and how they scope
# the query): "coords" uses lat/lon/radius, "store_keyword" uses a store search.
STOCK_BRAND_MODES = {
    StoreBrand.GS25.value: "coords",
    StoreBrand.SEVEN_ELEVEN.value: "store_keyword",
    StoreBrand.EMART24.value: "store_keyword",
}


def _render_product_detail(
    request: Request,
    product,
    db: Session,
    *,
    mock_result=None,
    checked_source_id: int | None = None,
    candidates=None,
    search_brand: str | None = None,
    search_keyword: str | None = None,
    searched: bool = False,
    stock=None,
    stock_source_id: int | None = None,
    stock_all=None,
    stock_all_keyword: str | None = None,
) -> HTMLResponse:
    location = get_search_location(db)
    return templates.TemplateResponse(
        "product_detail.html",
        {
            "request": request,
            "product": product,
            "brands": list(StoreBrand),
            "search_brands": SEARCH_BRANDS,
            "stock_brand_modes": STOCK_BRAND_MODES,
            "mock_result": mock_result,
            "checked_source_id": checked_source_id,
            "candidates": candidates,
            "search_brand": search_brand or (SEARCH_BRANDS[0].value if SEARCH_BRANDS else None),
            "search_keyword": search_keyword,
            "searched": searched,
            "stock": stock,
            "stock_source_id": stock_source_id,
            "stock_all": stock_all,
            "stock_all_keyword": stock_all_keyword,
            "location": location,
            "gs25_default_latitude": settings.gs25_default_latitude,
            "gs25_default_longitude": settings.gs25_default_longitude,
            "gs25_default_radius_meters": settings.gs25_default_radius_meters,
        },
    )


def _resolve_location(db: Session, submitted_keyword: str | None) -> tuple[str, float | None, float | None, int | None]:
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


async def _lookup_source_stock(source, keyword, latitude, longitude, radius_meters):
    """Dispatch a stock lookup for one saved source by brand."""
    if source.brand == StoreBrand.SEVEN_ELEVEN.value:
        return await check_seveneleven_stock(
            source.external_product_code,
            store_keyword=keyword,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
    if source.brand == StoreBrand.EMART24.value:
        return await check_emart24_stock(
            source.external_product_code,
            store_keyword=keyword,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
        )
    if source.brand == StoreBrand.GS25.value:
        if latitude is not None and longitude is not None:
            return await check_gs25_stock(
                source.external_product_code,
                latitude=latitude,
                longitude=longitude,
                radius_meters=radius_meters,
            )
        return await check_gs25_stock(source.external_product_code)
    return None


@router.get("", response_class=HTMLResponse)
async def products_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    products = list_products(db)
    return templates.TemplateResponse(
        "products.html",
        {
            "request": request,
            "products": products,
        },
    )


@router.get("/{product_id}", response_class=HTMLResponse)
async def product_detail_page(
    product_id: int,
    request: Request,
    checked_source_id: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    mock_result = None
    if checked_source_id is not None:
        source = next(
            (candidate for candidate in product.sources if candidate.id == checked_source_id),
            None,
        )
        if source is not None:
            mock_result = check_mock_inventory(source)

    return _render_product_detail(
        request,
        product,
        db,
        mock_result=mock_result,
        checked_source_id=checked_source_id,
        search_keyword=product.search_keyword,
    )


@router.post("/{product_id}/stock-all", response_class=HTMLResponse)
async def stock_all_action(
    product_id: int,
    request: Request,
    store_keyword: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Check real-time stock for every enabled source at once (7-Eleven / Emart24)."""
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    keyword, latitude, longitude, radius = _resolve_location(db, store_keyword)
    targets = [
        source
        for source in product.sources
        if source.enabled and source.brand in STOCK_BRAND_MODES
    ]

    # Limit concurrency so a batch of sources doesn't hammer the store APIs.
    semaphore = asyncio.Semaphore(3)

    async def _guarded(source):
        async with semaphore:
            return await _lookup_source_stock(source, keyword, latitude, longitude, radius)

    lookups = await asyncio.gather(*(_guarded(source) for source in targets))
    stock_all = list(zip(targets, lookups))
    return _render_product_detail(
        request,
        product,
        db,
        stock_all=stock_all,
        stock_all_keyword=keyword,
    )


@router.post("/{product_id}/watch-all")
async def watch_all_action(
    product_id: int,
    check_interval_seconds: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Register a restock watch for every enabled source of this product at once,
    using the current saved search location (keyword or map radius)."""
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    keyword, latitude, longitude, radius = _resolve_location(db, "")
    interval = check_interval_seconds or settings.watcher_default_interval_seconds
    create_watches_for_product(
        db,
        product_id=product_id,
        location_query=keyword,
        check_interval_seconds=max(interval, 30),
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius,
    )
    return RedirectResponse(url="/watches", status_code=303)


@router.get("/{product_id}/candidates.json")
async def candidates_json(
    product_id: int,
    brand: str,
    keyword: str,
    page: int = 1,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Search one page of candidates and return JSON for the picker modal.

    Marks candidates already saved for this product+brand as ``saved`` so the
    modal can disable them. ``has_more`` drives the "더 보기" button.
    """
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    existing = {
        source.external_product_code
        for source in product.sources
        if source.brand == brand
    }
    result = await search_products_page(brand, keyword, page=page)
    return JSONResponse(
        {
            "brand": brand,
            "keyword": keyword,
            "page": result.page,
            "has_more": result.has_more,
            "candidates": [
                {
                    "external_product_code": c.external_product_code,
                    "external_product_name": c.external_product_name,
                    "price": c.price,
                    "image_url": c.image_url,
                    "saved": c.external_product_code in existing,
                }
                for c in result.candidates
            ],
        }
    )


class _BulkCandidate(BaseModel):
    external_product_code: str
    external_product_name: str
    price: int | None = None
    image_url: str | None = None


class _BulkSaveRequest(BaseModel):
    brand: str
    candidates: list[_BulkCandidate]


@router.post("/{product_id}/candidates/save-bulk")
async def save_candidates_bulk(
    product_id: int,
    payload: _BulkSaveRequest,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Save multiple picked candidates as product sources, skipping duplicates."""
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    existing = {
        source.external_product_code
        for source in product.sources
        if source.brand == payload.brand
    }
    added = 0
    for candidate in payload.candidates:
        code = candidate.external_product_code.strip()
        if not code or code in existing:
            continue
        create_product_source(
            db,
            product_id,
            ProductSourceCreate(
                brand=payload.brand,
                external_product_code=code,
                external_product_name=candidate.external_product_name,
                image_url=candidate.image_url or None,
                price=candidate.price,
            ),
        )
        existing.add(code)
        added += 1

    return JSONResponse({"added": added})


@router.post("")
async def create_product_action(
    display_name: str = Form(...),
    search_keyword: str = Form(...),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    create_product(
        db,
        ProductCreate(
            display_name=display_name,
            search_keyword=search_keyword,
            memo=memo,
        ),
    )
    return RedirectResponse(url="/products", status_code=303)


@router.post("/{product_id}/sources")
async def create_product_source_action(
    product_id: int,
    brand: str = Form(...),
    external_product_code: str = Form(...),
    external_product_name: str = Form(...),
    barcode: str | None = Form(None),
    price: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    parsed_price = int(price) if price and price.strip() else None
    source = create_product_source(
        db,
        product_id,
        ProductSourceCreate(
            brand=brand,
            external_product_code=external_product_code,
            external_product_name=external_product_name,
            barcode=barcode,
            price=parsed_price,
        ),
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


def _find_source(product, source_id: int, expected_brand: StoreBrand):
    source = next(
        (candidate for candidate in product.sources if candidate.id == source_id),
        None,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Product source not found")
    if source.brand != expected_brand.value:
        raise HTTPException(
            status_code=400,
            detail=f"{expected_brand.value} 재고 조회는 {expected_brand.value} 상품코드에만 가능합니다.",
        )
    return source


@router.post("/{product_id}/sources/{source_id}/gs25-stock", response_class=HTMLResponse)
async def gs25_stock_action(
    product_id: int,
    source_id: int,
    request: Request,
    store_keyword: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    source = _find_source(product, source_id, StoreBrand.GS25)
    _, latitude, longitude, radius = _resolve_location(db, store_keyword)
    if latitude is not None and longitude is not None:
        stock = await check_gs25_stock(
            source.external_product_code,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius,
        )
    else:
        stock = await check_gs25_stock(source.external_product_code)
    return _render_product_detail(request, product, db, stock=stock, stock_source_id=source_id)


@router.post("/{product_id}/sources/{source_id}/seveneleven-stock", response_class=HTMLResponse)
async def seveneleven_stock_action(
    product_id: int,
    source_id: int,
    request: Request,
    store_keyword: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    source = _find_source(product, source_id, StoreBrand.SEVEN_ELEVEN)
    keyword, latitude, longitude, radius = _resolve_location(db, store_keyword)
    stock = await check_seveneleven_stock(
        source.external_product_code,
        store_keyword=keyword,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius,
    )
    return _render_product_detail(request, product, db, stock=stock, stock_source_id=source_id)


@router.post("/{product_id}/sources/{source_id}/emart24-stock", response_class=HTMLResponse)
async def emart24_stock_action(
    product_id: int,
    source_id: int,
    request: Request,
    store_keyword: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    source = _find_source(product, source_id, StoreBrand.EMART24)
    keyword, latitude, longitude, radius = _resolve_location(db, store_keyword)
    stock = await check_emart24_stock(
        source.external_product_code,
        store_keyword=keyword,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius,
    )
    return _render_product_detail(request, product, db, stock=stock, stock_source_id=source_id)


@router.post("/{product_id}/location")
async def set_location_action(
    product_id: int,
    latitude: float = Form(...),
    longitude: float = Form(...),
    radius_meters: int = Form(1000),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Save a map-picked location: reverse-geocode to a 구/동 keyword + store coords."""
    keyword = await reverse_geocode_keyword(latitude, longitude)
    if not keyword:
        # Geocoding failed; keep the previous keyword so store fetch can still seed.
        keyword = get_search_location(db).keyword
    set_search_location_coords(
        db,
        keyword=keyword,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
    )
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


@router.post("/{product_id}/sources/{source_id}/enable")
async def enable_product_source(
    product_id: int,
    source_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    set_product_source_enabled(db, source_id, True)
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


@router.post("/{product_id}/sources/{source_id}/disable")
async def disable_product_source(
    product_id: int,
    source_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    set_product_source_enabled(db, source_id, False)
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


@router.post("/{product_id}/sources/{source_id}/delete")
async def delete_product_source_action(
    product_id: int,
    source_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    delete_product_source(db, source_id)
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


@router.post("/{product_id}/sources/{source_id}/mock-check")
async def mock_check_product_source(product_id: int, source_id: int) -> RedirectResponse:
    return RedirectResponse(
        url=f"/products/{product_id}?checked_source_id={source_id}",
        status_code=303,
    )


@router.post("/{product_id}/enable")
async def enable_product(product_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    set_product_enabled(db, product_id, True)
    return RedirectResponse(url="/products", status_code=303)


@router.post("/{product_id}/disable")
async def disable_product(product_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    set_product_enabled(db, product_id, False)
    return RedirectResponse(url="/products", status_code=303)


@router.post("/{product_id}/delete")
async def delete_product_action(product_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_product(db, product_id)
    return RedirectResponse(url="/products", status_code=303)
