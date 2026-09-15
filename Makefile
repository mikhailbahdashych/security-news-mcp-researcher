.PHONY: dev-api dev-web test lint up

# The backend's port. Only ever a default here: a PORT in the environment or on
# the command line wins, and is exported to both recipes by make itself.
#
# Deliberately NOT passed to dev-api. The backend reads PORT through
# pydantic-settings, where the process environment beats the repo-root .env — so
# exporting this default would silently override a PORT written in .env.
PORT ?= 8000

dev-api: ## Run the FastAPI backend with reload on the configured PORT (default 8000)
	cd backend && uv run python -m app --reload

dev-web: ## Run the Vite dev server on :5173 (proxies /api to the backend's PORT)
	cd frontend && PORT=$(PORT) npm run dev

test: ## Run both test suites
	cd backend && uv run pytest
	cd frontend && npx vitest run

lint: ## Lint both halves
	cd backend && uv run ruff check .
	cd frontend && npm run lint

up: ## Build and run the whole app in Docker on the configured PORT (default 8000)
	docker compose up --build
