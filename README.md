# Security News MCP Researcher

A local-only, single-user web app for security engineers: an RSS security-news inbox,
an LLM research chat, and a meeting-notes generator. FastAPI backend, React SPA,
SQLite storage — nothing leaves your machine except the calls you ask it to make.

## Quickstart (Docker)

```sh
make up          # docker compose up --build
```

Then open <http://localhost:8000>. Data lives in the named `appdata` volume, so it
survives `docker compose down`.

## Local development

Two terminals:

```sh
make dev-api     # FastAPI with reload on :8000
make dev-web     # Vite dev server on :5173, proxying /api to :8000
```

Then open <http://localhost:5173>.

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.
Copy `.env.example` to `.env` if you want to override defaults.

## Tests and linting

```sh
make test        # cd backend && uv run pytest
make lint        # cd backend && uv run ruff check .
```

## Layout

| Path        | What it is                                              |
| ----------- | ------------------------------------------------------- |
| `backend/`  | FastAPI app (`app/`), tests, uv-managed dependencies     |
| `frontend/` | Vite + React + TypeScript SPA, built into `frontend/dist` |
| `docs/`     | Design notes                                             |

In Docker the SPA is built and served by the backend from `/app/static`, so the whole
app is one container on one port.
