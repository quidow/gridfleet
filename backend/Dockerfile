ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --no-dev --frozen

COPY backend/ .
COPY driver-packs/ /driver-packs/

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/lib/gridfleet/driver-packs \
    && chown -R appuser:appuser /app /driver-packs /var/lib/gridfleet

ENV GRIDFLEET_DRIVER_PACK_CURATED_DIR=/driver-packs/curated

USER appuser

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
