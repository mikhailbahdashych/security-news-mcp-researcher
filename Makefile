.PHONY: dev-api dev-web test lint up

dev-api: ## Run the FastAPI backend with reload on :8000
	cd backend && uv run uvicorn app.main:app --reload --port 8000

dev-web: ## Run the Vite dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

test: ## Run the backend test suite
	cd backend && uv run pytest

lint: ## Lint the backend
	cd backend && uv run ruff check .

up: ## Build and run the whole app in Docker on :8000
	docker compose up --build
