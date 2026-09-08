"""Punto de entrada de la aplicacion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__
from app.infra import db
from app.logging_config import configure_logging, get_logger
from app.settings import get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.is_production)

    db.init_engine(settings)
    log.info("aplicacion iniciada", env=settings.env, version=__version__)

    # El runner de jobs se engancha aca en la Fase 2.

    try:
        yield
    finally:
        await db.dispose_engine()
        log.info("aplicacion detenida")


app = FastAPI(
    title="Finance Consolidation",
    version=__version__,
    lifespan=lifespan,
    # La API no es publica: sin docs en produccion.
    docs_url=None if get_settings().is_production else "/docs",
    redoc_url=None,
    openapi_url=None if get_settings().is_production else "/openapi.json",
)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    """Liveness. No toca la base: responde aunque Postgres este caido."""
    return {"status": "ok", "version": __version__}


@app.get("/healthz/db", include_in_schema=False)
async def health_db() -> JSONResponse:
    """Readiness de la base, mas la verificacion de que RLS realmente aplica.

    Si el rol de la aplicacion pudiera saltear RLS, la app estaria "funcionando"
    pero sin aislamiento entre usuarios. Eso es una falla, no un warning: el
    endpoint devuelve 503.
    """
    try:
        info = await db.check_connection()
    # El detalle del fallo va al log, no a la respuesta: un healthcheck no filtra
    # el string de conexion ni la topologia de la base.
    except Exception as exc:
        log.error("healthcheck de base fallo", error=type(exc).__name__)
        return JSONResponse({"status": "error", "connected": False}, status_code=503)

    status_code = 200 if info["rls_enforced"] else 503
    return JSONResponse({"status": "ok" if status_code == 200 else "unsafe", **info}, status_code)
