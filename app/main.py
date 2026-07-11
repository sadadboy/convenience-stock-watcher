from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.health import router as health_router
from app.api.products import router as products_router
from app.api.watches import router as watches_router
from app.core.config import settings
from app.db.init_db import init_db
from app.db.session import get_db
from app.repositories.notifications import get_discord_webhook
from app.repositories.products import list_products
from app.repositories.watches import list_watches
from app.services.scheduler import start_scheduler, stop_scheduler

templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(health_router)
app.include_router(products_router)
app.include_router(watches_router)


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
        },
    )
