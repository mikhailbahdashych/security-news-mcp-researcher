.PHONY: dev-api dev-web test lint typecheck

# The three start-up knobs, passed to the backend as command-line flags. There is
# no .env file and no environment variable behind them: override on the command
# line, e.g. `make dev-api PORT=8012 DB=/tmp/scratch.db LOG=DEBUG`.
#
# DB empty means "the default", ./data/app.db relative to backend/.
PORT ?= 8000
DB   ?=
LOG  ?= INFO

dev-api: ## Run the FastAPI backend with reload on 127.0.0.1:$(PORT)
	cd backend && uv run python -m app --reload --port $(PORT) --log-level $(LOG) \
		$(if $(DB),--db-path $(DB))

dev-web: ## Run the Vite dev server on :5173 (proxies /api to the backend's PORT)
	cd frontend && PORT=$(PORT) npm run dev

test: ## Run both test suites
	cd backend && uv run pytest
	cd frontend && npx vitest run

lint: ## Lint both halves
	cd backend && uv run ruff check .
	cd frontend && npm run lint

typecheck: ## Type-check the SPA (tsc -b) — the only TypeScript check there is
	cd frontend && npx tsc -b
