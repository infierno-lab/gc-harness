.PHONY: db-up db-down migrate test serve

db-up:
	docker compose -f deploy/docker-compose.yml up -d

db-down:
	docker compose -f deploy/docker-compose.yml down

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

demo:
	uv run python scripts/demo.py

serve:
	uv run uvicorn core.api.app:app --port 8080 --reload
