# Story Readiness API. Built and run by compose.yaml; see "Run with containers" in the README.
FROM docker.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    STORIES_DB_PATH=/data/stories.db
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
