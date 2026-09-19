from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import login_redirect
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.products import router as products_router
from app.api.watches import router as watches_router
from app.core.config import settings
from app.core.templating import templates
from app.db.init_db import init_db
from app.db.session import SessionLocal, get_db
from app.models.device import TrustedDevice
from app.repositories.notifications import get_discord_webhook
from app.repositories.products import list_products
from app.repositories.watches import list_watches
from app.services.auth import COOKIE_NAME, cookie_max_age, find_device, touch_device
from app.services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(health_router)

# Reachable without a registered device.
_OPEN_PATHS = {"/login", "/health"}


@app.middleware("http")
async def require_trusted_device(request: Request, call_next):
    """Every page needs a registered device cookie (see app/services/auth.py).

    The watcher scheduler runs in-process, not over HTTP, so it is unaffected.
    """
    if request.url.path in _OPEN_PATHS:
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    ip = request.client.host if request.client else ""
    db = SessionLocal()
    try:
        device = find_device(db, token)
        refresh = device is not None and touch_device(db, device, ip)
    finally:
        db.close()

    if device is None:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return login_redirect(request)
        return JSONResponse({"detail": "기기 인증이 필요합니다."}, status_code=401)

    response = await call_next(request)
    if refresh:
        # Slide the expiry forward so a device in regular use never expires.
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age(),
            httponly=True,
            samesite="lax",
            secure=settings.auth_cookie_secure,
        )
    return response
app.include_router(products_router)
app.include_router(watches_router)


@app.exception_handler(StarletteHTTPException)
async def not_found_to_home(request: Request, exc: StarletteHTTPException):
    """Send browsers that open a wrong page address (404 on GET) back home.

    Form posts and JSON/API calls keep the normal error response.
    """
    if (
        exc.status_code == 404
        and request.method == "GET"
        and "text/html" in request.headers.get("accept", "")
    ):
        return RedirectResponse(url="/", status_code=303)
    return await http_exception_handler(request, exc)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    products = list_products(db)
    watches = list_watches(db)
    in_stock = sum(1 for w in watches if w.last_stock_status == "in_stock")
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "product_count": len(products),
            "watch_count": len(watches),
            "in_stock_count": in_stock,
            "discord_configured": bool(get_discord_webhook(db)),
            "watcher_enabled": settings.watcher_enabled,
            "device_count": db.scalar(select(func.count(TrustedDevice.id))) or 0,
        },
    )
