# 24시간 상시 구동 가이드

입고 감시는 앱이 계속 떠 있어야 동작합니다. 노트북이 자거나 꺼지면 감시도 멈춥니다.

## 1. 컨테이너 자동 재시작 (설정 완료)

`docker-compose.yml`에 `restart: unless-stopped`를 넣어서:

- 앱이 크래시해도 자동 재시작
- Docker Desktop / 호스트가 재부팅돼도 컨테이너 자동 복구 (Docker가 실행 중이면)
- DB가 준비된 뒤에 앱이 뜨도록 healthcheck 연결

적용:

```bash
docker compose up --build -d
```

## 2. 맥에서 계속 돌리기 (임시책)

노트북을 상시 서버로 쓰려면 **잠들지 않게** 해야 합니다.

- **전원 연결** 후, 다음으로 잠자기 방지 (터미널을 켜둔 동안 유지):

  ```bash
  caffeinate -s        # AC 전원 연결 상태에서 시스템 잠자기 방지
  ```

  또는 영구 설정(관리자):

  ```bash
  sudo pmset -c sleep 0            # AC 전원일 때 시스템 잠자기 안 함
  sudo pmset -c disablesleep 1     # 뚜껑 닫아도 안 자게 (클램셸)
  ```

  되돌리기: `sudo pmset -c disablesleep 0 && sudo pmset -c sleep 10`

- **Docker Desktop**: 설정 → General → "Start Docker Desktop when you sign in" 켜기.
  로그인 시 Docker가 뜨고, `restart: unless-stopped`가 컨테이너를 자동으로 올립니다.

> 한계: 맥이 물리적으로 꺼지거나 네트워크가 끊기면 그동안은 감시가 멈춥니다. 노트북을 24시간 켜두는 건 발열·전력 부담이 있어 임시책입니다.

## 3. 제대로 하려면: 상시 켜진 기기 (권장)

가장 확실한 건 **끄지 않는 작은 기기**에 올리는 것:

- **미니 PC / 라즈베리파이(4 이상)**: 집에 두고 24시간. Docker 설치 후 이 저장소 그대로 `docker compose up -d`. 전력 적고 조용함.
- **클라우드 VPS**(월 몇 천 원대): Oracle Cloud 무료 tier, 라이트세일, Vultr 등. 리눅스 + Docker → 동일하게 실행. 공인 IP로 어디서든 접속.
- **PaaS**(Fly.io / Railway / Render): `Dockerfile`이 있어 배포 쉬움. 단, Postgres를 그 플랫폼의 관리형 DB로 붙이고 `DATABASE_URL`을 바꿔야 함.

어느 방식이든 **디스코드 알림은 그대로 동작**합니다(밖으로 나가는 웹훅이라 공인 IP 불필요).

## 4. 살아있는지 확인

```bash
# 컨테이너 상태 (Up / restart policy)
docker compose ps

# 헬스체크
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/health

# 스케줄러가 도는지 (최근 이벤트가 계속 쌓이는지)
#  -> http://localhost:8000/watches 하단 "최근 이벤트"
```

재부팅 테스트: Docker Desktop을 종료했다 다시 켜면 컨테이너가 자동으로 올라와야 정상입니다.
