# ─── build ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# pikepdf compila contra qpdf; pdfplumber trae su propia rueda.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libqpdf-dev \
    && rm -rf /var/lib/apt/lists/*

# Capa de dependencias separada del codigo: cambia mucho menos seguido.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY templates ./templates
COPY static ./static
RUN uv sync --frozen --no-dev

# ─── runtime ─────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libqpdf29 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
