# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the single-page app
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web

WORKDIR /web

# Dependencies first so the npm layer is reused when only sources change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — runtime: FastAPI serving the API and the built SPA
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

# Install the locked runtime dependencies system-wide (no venv in the image).
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && rm requirements.txt pyproject.toml uv.lock

COPY backend/app ./app
COPY --from=web /web/dist ./static

ENV DB_PATH=/data/app.db \
    STATIC_DIR=/app/static \
    PORT=8000

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
