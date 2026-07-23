# syntax=docker/dockerfile:1.7

# The backend serves the SPA itself (app.frontend on repo-root dist/), so the
# image carries its own frontend build — one artifact, no tag-sync with a
# separately-shipped UI.
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS spa
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# Debian package revisions vary by architecture; the base image is immutable and
# Dependabot advances its digest so rebuilds pick up the matching security set.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker.io/astral/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY README.md LICENSE ./
COPY backend/ ./backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Same height as a dev checkout: repo-root dist/ (vite's outDir).
COPY --from=spa /app/dist ./dist

ENV DRUKS_DATA_DIR=/app/data
RUN mkdir -p /app/data

# We run as a non-root, arbitrary uid (the deploy user — see the compose
# `user:`). Such a uid has no /etc/passwd entry, so getpass.getuser() — which
# asyncssh calls on every SSH connect — would raise. Declaring a username lets
# it resolve from the environment instead. Cosmetic: the real SSH user is
# passed per connection.
ENV USER=druks LOGNAME=druks

EXPOSE 8001
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "druks.api.app:app", "--host", "127.0.0.1", "--port", "8001"]
