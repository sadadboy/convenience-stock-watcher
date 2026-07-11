# Convenience Stock Watcher

Convenience store inventory watcher for GS25, CU, 7-Eleven, and Emart24.

## Planned Stack

- Python
- FastAPI
- PostgreSQL
- APScheduler
- Docker Compose
- Discord Webhook
- Telegram Bot

## Current Status

Initial design and scaffold.

## What You Need To Do First

1. Copy `.env.example` to `.env`.
2. Fill notification values when you are ready.
3. Run the app with Docker Compose.
4. Open the dashboard.

```bash
cp .env.example .env
docker compose up --build
```

For the first milestone, Discord is the recommended notification channel because a webhook is simpler than Telegram bot setup.

## Start Locally

```bash
docker compose up --build
```

Then open:

```text
http://localhost:8000
```

## Documents

- `docs/PRD.md`
- `docs/DATA_MODEL.md`
- `docs/SCHEDULER.md`

## Next Development Tasks

1. Add SQLAlchemy models and Alembic migrations.
2. Add product CRUD screens.
3. Add watch rule CRUD screens.
4. Add scheduler loop with mock adapter.
5. Add Discord alert sender.
6. Add first real store adapter.
