# Multi-stage build. `uv` is used for dependency resolution because it is an
# order of magnitude faster than pip and produces a reproducible install.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

# libpq is needed by psycopg; build-essential is needed to compile lightgbm/xgboost wheels
# on platforms without prebuilt binaries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy only the manifest first so the dependency layer is cached and does not
# rebuild on every source edit.
COPY pyproject.toml ./
RUN uv pip install --system ".[ml]"

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
