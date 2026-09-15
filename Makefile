.PHONY: dev-api dev-web test lint up

dev-api: ## Run the FastAPI backend with reload on the configured PORT (default 8000)
	cd backend && uv run python -m app --reload

dev-web: ## Run the Vite dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

test: ## Run both test suites
	cd backend && uv run pytest
	cd frontend && npx vitest run

lint: ## Lint both halves
	cd backend && uv run ruff check .
	cd frontend && npm run lint

up: ## Build and run the whole app in Docker on the configured PORT (default 8000)
	docker compose up --build
