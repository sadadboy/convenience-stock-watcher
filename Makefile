.PHONY: dev up down logs

dev:
	uvicorn app.main:app --reload

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f app
