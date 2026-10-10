# Cloud Run container for the Jenny agent. Build from the REPO ROOT:
#   docker build -t <IMAGE> .
FROM python:3.12-slim

# uv: fast, reproducible installs from the committed uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Install deps first (cached layer) from the lockfile — runtime deps only, no dev.
COPY agent/pyproject.toml agent/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the app code (persona.md lives in agent/, so it's copied here too).
COPY agent/ .
ENV PERSONA_PATH=/app/persona.md
ENV PATH="/app/.venv/bin:$PATH"

ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
