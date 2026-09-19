from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.templating import templates
from app.db.session import get_db
from app.models.device import TrustedDevice
from app.services.auth import (
    COOKIE_NAME,
    auth_configured,
    check_password,
    cookie_max_age,
    find_device,
    guess_device_name,
    is_locked_out,
    register_device,
    safe_next,
)

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _login_page(request: Request, *, next_path: str, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": next_path,
            "error": error,
            "configured": auth_configured(),
            "device_name": guess_device_name(request.headers.get("user-agent", "")),
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)) -> HTMLResponse:
    if find_device(db, request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(url=safe_next(next), status_code=303)
    return _login_page(request, next_path=safe_next(next))


@router.post("/login")
async def login_action(
    request: Request,
    password: str = Form(...),
    device_name: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    next_path = safe_next(next)
    if not auth_configured():
        return _login_page(request, next_path=next_path, status_code=503)
    if is_locked_out(ip):
        return _login_page(
            request,
            next_path=next_path,
            error="비밀번호를 여러 번 틀려 15분 동안 잠겼습니다.",
            status_code=429,
        )
    if not check_password(ip, password):
        return _login_page(request, next_path=next_path, error="비밀번호가 틀렸습니다.", status_code=401)

    token = register_device(
        db,
        name=device_name or guess_device_name(request.headers.get("user-agent", "")),
        user_agent=request.headers.get("user-agent", ""),
        ip=ip,
    )
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=cookie_max_age(),
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return response


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current = find_device(db, request.cookies.get(COOKIE_NAME))
    devices = list(db.scalars(select(TrustedDevice).order_by(TrustedDevice.created_at.desc())))
    return templates.TemplateResponse(
        "devices.html",
        {"request": request, "devices": devices, "current_id": current.id if current else None},
    )


@router.post("/devices/revoke")
async def revoke_devices_action(
    request: Request,
    device_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Sign the checked devices out (the current one too, if checked)."""
    current = find_device(db, request.cookies.get(COOKIE_NAME))
    signed_out_self = False
    for device in db.scalars(select(TrustedDevice).where(TrustedDevice.id.in_(device_ids))):
        signed_out_self = signed_out_self or (current is not None and device.id == current.id)
        db.delete(device)
    db.commit()
    if signed_out_self:
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response
    return RedirectResponse(url="/devices", status_code=303)


@router.post("/logout")
async def logout_action(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Forget this device: it will need the password again."""
    device = find_device(db, request.cookies.get(COOKIE_NAME))
    if device is not None:
        db.delete(device)
        db.commit()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


def login_redirect(request: Request) -> RedirectResponse:
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)
