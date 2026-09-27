# The voice server: Druks's program on Pipecat that carries phone calls. It runs
# beside Druks in the Compose stack, under the voice profile. The build context is
# the repository's voice/ directory.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

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

COPY druks_voice/ ./druks_voice/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER nobody
EXPOSE 8002
CMD ["uvicorn", "druks_voice.app:app", "--host", "127.0.0.1", "--port", "8002"]
