# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the single-page app
#
# KEEP THIS STAGE AND THE RUNTIME STAGE ON THE SAME DEBIAN RELEASE (bookworm).
# The runtime stage copies this stage's `node` binary so that stdio MCP servers
# can be launched with `npx`, and that binary is dynamically linked against this
# release's glibc. Bumping one stage without the other breaks `npx` inside the
# image with an obscure dynamic-loader error rather than anything that names Node.
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
# Same Debian release as the `web` stage above — see the note there before bumping.
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

# Node, purely so stdio MCP servers configured as `npx -y <package>` can run. The
# binary plus npm's own JS is all that is needed — installing the `nodejs` apt
# package on top would duplicate ~50 MB for nothing. npm and npx ship as symlinks
# into /usr/local/lib/node_modules/npm/bin, so they are recreated here rather than
# copied (copying the bin directory alone yields dangling links).
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version && npx --version && uvx --version

COPY backend/app ./app
COPY --from=web /web/dist ./static

# HOME and NPM_CONFIG_CACHE both point into the data volume so that `npx -y ...`
# has somewhere writable to cache packages, and so that cache survives a
# `docker compose up --build` instead of re-downloading on every first connect.
#
# HOME is the load-bearing one. A stdio MCP server does NOT inherit this process's
# environment: the SDK gives the subprocess an allow-list (HOME, LOGNAME, PATH,
# SHELL, TERM, USER) plus whatever the server's own `env` block adds. So
# NPM_CONFIG_CACHE never reaches npx — npm falls back to $HOME/.npm, which is why
# HOME has to be the writable path.
ENV DB_PATH=/data/app.db \
    STATIC_DIR=/app/static \
    PORT=8000 \
    HOME=/data \
    NPM_CONFIG_CACHE=/data/.npm

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
