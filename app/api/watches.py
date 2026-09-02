from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.templating import templates
from app.domain.alerts import WatchStatus
from app.repositories.location import get_search_location, resolve_location
from app.repositories.notifications import get_discord_webhook, set_discord_webhook
from app.repositories.watches import (
    LOCATION_KEY_NEW,
    create_watch,
    create_watches_for_product,
    decode_location_key,
    delete_watch,
    delete_watches,
    get_watch,
    list_watchable_products,
    list_watchable_sources,
    list_watch_locations,
    recent_events,
    set_watch_status,
    set_watches_status,
    update_watches_settings,
)
from app.db.session import get_db
from fastapi.responses import JSONResponse
from app.services.notifications import send_discord_message
from app.services.store_adapter import StoreBrand
from app.services.watcher import check_watch

router = APIRouter(prefix="/watches", tags=["watches"])

# How each brand scopes a watch location (for UI hints).
LOCATION_MODES = {
    StoreBrand.SEVEN_ELEVEN.value: "매장 키워드 (예: 강남, 동작구청)",
    StoreBrand.EMART24.value: "매장 키워드 (예: 강남, 동작구청)",
    StoreBrand.GS25.value: "위도,경도,반경m (예: 37.5665,126.978,1000)",
}


def _mask_webhook(url: str | None) -> str:
    if not url:
        return ""
    return url[:36] + "…" if len(url) > 36 else url


@router.get("", response_class=HTMLResponse)
async def watches_page(
    request: Request,
    test: str | None = None,
    added: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    webhook = get_discord_webhook(db)
    return templates.TemplateResponse(
        "watches.html",
        {
            "request": request,
            "locations": list_watch_locations(db),
            "sources": list_watchable_sources(db),
            "products": list_watchable_products(db),
            "saved_location": get_search_location(db),
            "location_key_new": LOCATION_KEY_NEW,
            "events": recent_events(db),
            "location_modes": LOCATION_MODES,
            "default_interval": settings.watcher_default_interval_seconds,
            "watcher_enabled": settings.watcher_enabled,
            "discord_configured": bool(webhook),
            "discord_masked": _mask_webhook(webhook),
            "test_result": test,
            "added": added,
        },
    )


@router.post("/notifications/discord")
async def save_discord_webhook(
    webhook_url: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    set_discord_webhook(db, webhook_url)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/notifications/discord/test")
def test_discord_webhook(db: Session = Depends(get_db)) -> RedirectResponse:
    webhook = get_discord_webhook(db)
    if not webhook:
        return RedirectResponse(url="/watches?test=nourl", status_code=303)
    ok, _ = send_discord_message(
        webhook,
        "✅ 테스트 알림",
        "편의점 재고 알리미 디스코드 연결이 정상입니다. 입고 시 이렇게 알림이 옵니다.",
        color=0x3498DB,
    )
    return RedirectResponse(url=f"/watches?test={'ok' if ok else 'fail'}", status_code=303)


@router.post("")
async def create_watch_action(
    product_source_id: int = Form(...),
    location_query: str = Form(...),
    check_interval_seconds: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    watch = create_watch(
        db,
        product_source_id=product_source_id,
        location_query=location_query,
        check_interval_seconds=max(check_interval_seconds, 30),
    )
    if watch is None:
        raise HTTPException(status_code=404, detail="Product source not found")
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/product")
async def create_product_watches_action(
    product_id: int = Form(...),
    location_key: str = Form(LOCATION_KEY_NEW),
    location_query: str = Form(""),
    check_interval_seconds: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Register a watch for every enabled source of one product, in one go.

    ``location_key`` picks an existing watch location so the new watches land in
    that list exactly (same keyword/coords/radius); otherwise the typed keyword
    is resolved the same way the product screen does it.
    """
    existing = decode_location_key(location_key) if location_key != LOCATION_KEY_NEW else None
    if existing is not None:
        keyword, latitude, longitude, radius = existing
    else:
        keyword, latitude, longitude, radius = resolve_location(db, location_query)
    interval = check_interval_seconds or settings.watcher_default_interval_seconds
    created = create_watches_for_product(
        db,
        product_id=product_id,
        location_query=keyword,
        check_interval_seconds=max(interval, 30),
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius,
    )
    return RedirectResponse(url=f"/watches?added={created}", status_code=303)


@router.post("/{watch_id}/check")
def check_watch_now(watch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    # Sync def: check_watch runs the async adapter via asyncio.run in a worker thread.
    watch = get_watch(db, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch not found")
    check_watch(db, watch)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/{watch_id}/check.json")
def check_watch_json(watch_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    """Check one watch and return a compact result (for the "지금 전체 확인" progress UI)."""
    watch = get_watch(db, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch not found")
    check_watch(db, watch)
    db.refresh(watch)
    return JSONResponse(
        {
            "watch_id": watch_id,
            "status": watch.status,
            "stock_status": watch.last_stock_status,
            "quantity": watch.last_stock_quantity,
            "summary": watch.last_in_stock_summary or watch.last_error or "",
        }
    )


@router.post("/group/pause")
async def pause_group(watch_ids: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    set_watches_status(db, watch_ids, WatchStatus.DISABLED)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/group/resume")
async def resume_group(watch_ids: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    set_watches_status(db, watch_ids, WatchStatus.ACTIVE)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/group/delete")
async def delete_group(watch_ids: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    delete_watches(db, watch_ids)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/group/settings")
async def update_group_settings(
    watch_ids: str = Form(...),
    check_interval_seconds: int = Form(...),
    max_alert_count: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    update_watches_settings(
        db,
        watch_ids,
        check_interval_seconds=check_interval_seconds,
        max_alert_count=max_alert_count,
    )
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/{watch_id}/pause")
async def pause_watch(watch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    set_watch_status(db, watch_id, WatchStatus.DISABLED)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/{watch_id}/resume")
async def resume_watch(watch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    set_watch_status(db, watch_id, WatchStatus.ACTIVE)
    return RedirectResponse(url="/watches", status_code=303)


@router.post("/{watch_id}/delete")
async def delete_watch_action(watch_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_watch(db, watch_id)
    return RedirectResponse(url="/watches", status_code=303)
