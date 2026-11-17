# 다른 앱에서 기기 인증 같이 쓰기

이 앱(convenience-stock-watcher, 호스트 포트 8090)에 **비밀번호로 한 번 등록한 기기**를
같은 서버의 다른 앱에서도 그대로 통과시키는 방법입니다. 다른 앱에는 비밀번호를 따로 두지 않습니다.

> 다른 앱 폴더에서 Claude 에게 맡길 때:
> "`/home/ubuntu/convenience-stock-watcher/docs/SHARED_DEVICE_AUTH.md` 대로 이 앱에 기기 인증 붙여줘"

## 동작 원리

1. 이 앱에 로그인하면 브라우저에 `csw_device` 쿠키(365일, HttpOnly, SameSite=Lax, Secure 아님)가 저장된다.
2. **쿠키는 포트를 구분하지 않는다.** 같은 주소(예: `http://<서버IP>:8090`)에서 받은 쿠키는
   `http://<서버IP>:8000` 같은 다른 포트로 접속할 때도 함께 전송된다.
3. 다른 앱은 그 쿠키를 그대로 이 앱의 `GET /auth/device-check` 로 보내서 확인한다.
   - `204` → 등록된 기기 → 통과
   - `401` 또는 연결 실패 → 차단 (fail-closed)
4. 확인 결과는 5분 동안 캐시한다. 그래서 이 앱의 `/devices` 에서 기기를 삭제하면 다른 앱에서는 최대 5분 뒤 차단된다.

`/auth/device-check` 는 `app/api/auth.py` 에 있다. 기존 `require_trusted_device` 미들웨어를 통과해야만
이 주소에 도달하므로, 따로 검사하는 코드는 없다.

## 전제 조건

- 사용자가 **두 앱에 같은 주소로 접속**해야 한다. 둘 다 IP로 접속하거나 둘 다 같은 도메인으로 접속해야 하며,
  한쪽은 IP, 한쪽은 도메인이면 쿠키가 공유되지 않는다.
- 다른 앱이 HTTP(평문)로 서비스되어야 한다. 이 앱의 쿠키가 Secure 가 아니므로 HTTP·HTTPS 어느 쪽에서든 전송되지만,
  `AUTH_COOKIE_SECURE=true` 로 바꾸면 HTTP 앱에는 쿠키가 실리지 않는다.
- 다른 앱 컨테이너는 **이 앱의 Docker 네트워크로 접근**해야 한다.
  호스트의 8090 포트(`172.x.0.1:8090`)로는 ufw(INPUT DROP) 때문에 컨테이너에서 접속되지 않는다.
- 이 앱이 꺼져 있으면 다른 앱도 들어갈 수 없다.

## 적용 방법

### 1. docker-compose.yml

```yaml
services:
  <서비스명>:
    environment:
      - DEVICE_AUTH_URL=http://app:8000/auth/device-check
      - DEVICE_LOGIN_PORT=8090
    networks:
      - default
      - csw

networks:
  csw:
    external: true
    name: convenience-stock-watcher_default
```

`app` 은 이 앱의 compose 서비스 이름(네트워크 별칭)이고, `8000` 은 컨테이너 안쪽 포트다.

### 2-A. FastAPI / Starlette

실제 적용 예는 `/home/ubuntu/valet/valet_bot/web.py` 의 `require_trusted_device` 다.

```python
import hashlib, html, os, time, urllib.request
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

_AUTH_URL = os.environ.get("DEVICE_AUTH_URL", "")
_LOGIN_PORT = os.environ.get("DEVICE_LOGIN_PORT", "8090")
_DEVICE_COOKIE = "csw_device"
_AUTH_CACHE_TTL = 300
_auth_cache: dict[str, float] = {}  # sha256(token) -> 만료 시각(monotonic)


def _check_device(token: str) -> bool:
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    if _auth_cache.get(key, 0.0) > now:
        return True
    req = urllib.request.Request(
        _AUTH_URL,
        headers={"Cookie": f"{_DEVICE_COOKIE}={token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = resp.status == 204
    except Exception:
        ok = False  # fail-closed
    if ok:
        for k in [k for k, exp in _auth_cache.items() if exp <= now]:
            _auth_cache.pop(k, None)
        _auth_cache[key] = now + _AUTH_CACHE_TTL
    return ok


def _login_page(hostname: str | None) -> str:
    host = hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    login = html.escape(f"http://{host}:{_LOGIN_PORT}/login", quote=True)
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>기기 인증 필요</title><body style='font-family:sans-serif;padding:16px'>"
        "<h3>등록된 기기만 접속할 수 있습니다</h3>"
        f'<p><a href="{login}">{login}</a> 에서 기기 인증을 마친 뒤 이 페이지를 새로고침하세요.</p>'
    )


@app.middleware("http")
async def require_trusted_device(request: Request, call_next):
    token = request.cookies.get(_DEVICE_COOKIE)
    if _AUTH_URL and token and await run_in_threadpool(_check_device, token):
        return await call_next(request)
    return HTMLResponse(_login_page(request.url.hostname), status_code=401)
```

### 2-B. Flask

`_check_device` 와 `_login_page` 는 2-A 와 같다(FastAPI import 만 빼면 된다).

```python
from flask import request

@app.before_request
def require_trusted_device():
    token = request.cookies.get(_DEVICE_COOKIE)
    if _AUTH_URL and token and _check_device(token):
        return None
    return _login_page(request.host.rsplit(":", 1)[0].strip("[]")), 401
```

### 주의할 점

- 미들웨어는 **모든 경로**를 막는다. 정적 파일, 스크린샷 등도 포함된다. healthcheck 처럼 열어 둘 경로가 있으면
  해당 경로만 예외로 둔다.
- 앱에 자체 로그인이 있었다면 그 로그인을 제거하거나, 둘 다 통과해야 하도록 할지 정한다.
- 로그인 안내 링크의 host 값은 요청의 Host 헤더에서 오므로 반드시 `html.escape` 한다(위 코드에 반영됨).
- `DEVICE_AUTH_URL` 이 비어 있으면 모든 요청을 차단한다. 환경변수를 빠뜨리면 아예 들어갈 수 없게 된다.

## 적용 후 확인

```bash
# 이 앱: 쿠키 없으면 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Accept: application/json' http://127.0.0.1:8090/auth/device-check

# 다른 앱(<포트>): 쿠키 없음 / 가짜 쿠키 → 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<포트>/
curl -s -o /dev/null -w '%{http_code}\n' -b 'csw_device=bogus' http://127.0.0.1:<포트>/

# 컨테이너가 이 앱 네트워크에 붙었는지
docker inspect <컨테이너> -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

등록된 기기로 통과되는지는 브라우저에서 `http://<서버IP>:8090` 에 로그인한 뒤, 같은 IP의 다른 앱 포트로
접속해서 확인한다.

## 이 방식을 쓰는 앱

| 앱 | 포트 | 적용 위치 |
|---|---|---|
| valet | 8000 | `/home/ubuntu/valet/valet_bot/web.py`, `/home/ubuntu/valet/docker-compose.yml` |
