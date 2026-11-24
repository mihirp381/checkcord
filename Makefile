.PHONY: setup test test-integration lint typecheck format dev-up dev-down up down run-api run-consumer

setup:
	uv sync
	uv run pre-commit install

test:
	uv run pytest -m "not integration"

test-integration: dev-up
	uv run pytest -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy .

format:
	uv run ruff format .
	uv run ruff check --fix .

dev-up:
	docker compose up -d --wait postgres redis
	uv run checkcord migrate

dev-down:
	docker compose down -v

up:
	@test -f .env || { echo "missing .env: cp .env.example .env and fill it in"; exit 1; }
	docker compose up -d --build --wait

down:
	docker compose down

run-api:
	uv run checkcord api

run-consumer:
	uv run checkcord worker
