# Oracle Cloud (Always Free) 배포 가이드

Oracle Cloud의 **Always Free ARM 인스턴스(Ampere A1)** 에 올려 24시간 구동합니다.
이 앱은 코드 변경 없이 ARM에서 그대로 빌드됩니다.

> 보안 요점: 이 앱은 **로그인/인증이 없습니다.** 그래서 웹 UI 포트(8000)를 **인터넷에 열지 않고**, 필요할 때만 **SSH 터널**로 접속합니다. 감시·디스코드 알림은 서버에서 알아서 돌기 때문에 UI를 상시 열어둘 필요가 없습니다.

---

## 1. 인스턴스 생성 (OCI 콘솔)

1. Oracle Cloud 콘솔 → **Compute → Instances → Create instance**
2. **Image**: Canonical **Ubuntu 22.04**
3. **Shape**: **Ampere / VM.Standard.A1.Flex** (Always Free 범위: 예 1 OCPU / 6 GB면 충분)
4. **SSH keys**: 본인 공개키 등록 (없으면 새로 생성 후 개인키 보관)
5. 생성 → **Public IP** 확인

## 2. 접속 & Docker 설치

```bash
ssh ubuntu@<PUBLIC_IP>

# Docker + compose 플러그인
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# 그룹 적용 위해 재접속
exit
ssh ubuntu@<PUBLIC_IP>
docker version && docker compose version   # 확인
```

## 3. 코드 올리기

**방법 A — 로컬에서 scp (가장 간단)**  ※ 로컬 맥에서 실행:

```bash
# 프로젝트 폴더 통째로 전송 (.git, __pycache__ 등은 알아서 제외해도 무방)
scp -r ~/Documents/codexProjects/convenience-stock-watcher ubuntu@<PUBLIC_IP>:~/
```

**방법 B — git** (GitHub 등에 올려둔 경우): `git clone <repo-url>`

## 4. 환경 파일 & 실행

```bash
cd ~/convenience-stock-watcher
cp .env.example .env          # 기본값으로 충분 (DATABASE_URL 등 그대로)
docker compose up --build -d  # ARM에서 네이티브 빌드 (몇 분)
docker compose ps             # app, db 가 Up / db (healthy) 인지 확인
```

`restart: unless-stopped`가 걸려 있어 **인스턴스 재부팅 시 자동으로 다시 뜹니다.**

## 5. 방화벽 = 아무 포트도 열지 않음 (권장)

OCI **Security List / NSG**에서 **8000·5432를 열지 마세요.** 대신 UI는 SSH 터널로:

```bash
# 로컬 맥에서 (접속하고 싶을 때만)
ssh -L 8000:localhost:8000 ubuntu@<PUBLIC_IP>
# 그 상태로 브라우저에서 http://localhost:8000  → 서버의 UI에 안전하게 접속
```

- 이렇게 하면 앱이 인터넷에 노출되지 않습니다.
- **디스코드 알림은 SSH 터널과 무관하게 항상 작동**합니다(서버에서 밖으로 나가는 웹훅).

> 굳이 브라우저로 공개 접속하고 싶다면: 최소한 Security List의 8000을 **본인 집 IP만** 허용하도록 좁히세요. 아무에게나 열면 남이 사장님 감시를 보고 수정할 수 있습니다. (원하시면 나중에 간단 로그인만 붙여드릴 수 있습니다.)

## 6. 디스코드 웹훅 설정

클라우드 DB는 **새(빈) 상태**로 시작합니다. SSH 터널로 UI 접속 →
`/watches` → "🔔 디스코드 알림 설정"에 웹훅 URL 저장 → 테스트 전송.

## 7. 기존 감시 데이터 그대로 옮기기 (선택)

로컬의 150개 감시·상품을 클라우드로 이관하려면 (안 하면 클라우드에서 새로 등록):

```bash
# (로컬 맥) 덤프 생성
cd ~/Documents/codexProjects/convenience-stock-watcher
docker compose exec -T db pg_dump -U stock -d stock > stock_backup.sql
scp stock_backup.sql ubuntu@<PUBLIC_IP>:~/convenience-stock-watcher/

# (서버) DB만 먼저 띄우고, 앱이 테이블 만들기 전에 복원
cd ~/convenience-stock-watcher
docker compose up -d db
sleep 8
cat stock_backup.sql | docker compose exec -T db psql -U stock -d stock
docker compose up -d          # 앱 기동 (init_db는 기존 테이블 그대로 인식)
```

## 8. 확인

```bash
docker compose ps                         # Up / healthy
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/health   # 서버에서 200
docker compose logs app --tail=20         # 스케줄러 동작 로그
```

재부팅 테스트: `sudo reboot` 후 다시 접속해 `docker compose ps`가 자동으로 Up이면 성공.

## 참고 / 주의
- **비용**: Always Free 범위(ARM 4 OCPU/24GB 한도 내)면 무료. 인스턴스 shape를 Always Free 범위로 잡으세요.
- **DB 비밀번호**: 기본 `stock/stock`. 5432를 안 여니 위험은 낮지만, 신경 쓰이면 `docker-compose.yml`·`.env`의 `DATABASE_URL`에서 바꾸세요.
- **부하**: 감시가 많으면 `WATCHER_MAX_CHECKS_PER_TICK`(기본 25)로 분산됩니다. 필요 시 `.env`에서 조정.
