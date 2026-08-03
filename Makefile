.PHONY: dev up down logs test lint

dev:
	@echo "请分别启动后端与前端，详见 README.md"

up:
	docker compose --env-file .env -f infra/docker-compose.yml up --build -d

down:
	docker compose --env-file .env -f infra/docker-compose.yml down

logs:
	docker compose --env-file .env -f infra/docker-compose.yml logs -f

test:
	cd apps/api && python -m pytest
	cd apps/web && npm run build

lint:
	cd apps/api && python -m ruff check .
	cd apps/web && npm run typecheck
