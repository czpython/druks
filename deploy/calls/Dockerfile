# The calls server: Druks's program on Pipecat that carries phone calls. It runs
# beside Druks as its own Compose service. The build context is the repository's
# druks-calls/ directory.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

COPY --from=docker.io/astral/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82 /uv /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY calls/ ./calls/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER nobody
EXPOSE 8002
CMD ["uvicorn", "calls.app:app", "--host", "127.0.0.1", "--port", "8002"]
