"""Punto de entrada de la aplicacion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse, Response

from app import __version__
from app.api import admin
from app.deps import SESSION_CLEAR_ATTR
from app.infra import db
from app.jobs import runner as job_runner
from app.logging_config import configure_logging, get_logger
from app.security import jwt as jwt_verify
from app.security.csrf import CSRFError
from app.security.exceptions import NotAuthenticatedError, NotAuthorizedError
from app.settings import get_settings
from app.web import auth as web_auth
from app.web import dashboard as web_dashboard
from app.web import documents as web_documents
from app.web import review as web_review
from app.web import settings_page as web_settings
from app.web.middleware import SessionCookieMiddleware
from app.web.templates import is_htmx, render

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.is_production)

    db.init_engine(settings)
    # La cache de JWKS no debe sobrevivir a un cambio de configuracion entre
    # arranques (por ejemplo, apuntar a otro proyecto de Supabase).
    jwt_verify.reset_jwks_cache()
    log.info("aplicacion iniciada", env=settings.env, version=__version__)

    # El runner vive en el mismo proceso que la web. Para este volumen alcanza, y
    # mudarlo a `python -m app.worker` no requiere cambios: la toma de trabajos
    # ya usa `for update skip locked`.
    await job_runner.start_runner(settings)

    try:
        yield
    finally:
        await job_runner.stop_runner()
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

# Renueva la cookie cuando una dependencia refresco el access token.
app.add_middleware(SessionCookieMiddleware)

app.include_router(web_auth.router)
app.include_router(web_dashboard.router)
app.include_router(web_settings.router)
app.include_router(web_documents.router)
app.include_router(web_review.router)
app.include_router(admin.router)


@app.exception_handler(NotAuthenticatedError)
async def handle_not_authenticated(request: Request, exc: NotAuthenticatedError) -> Response:
    """Sin sesion: al login si es una pagina, 401 si es una llamada de datos.

    Un 303 hacia el login en respuesta a una request de HTMX terminaria con el
    formulario de login inyectado dentro de la pagina, asi que ahi se responde
    401 con el header que HTMX usa para redirigir la ventana entera.
    """
    if exc.clear_cookie:
        # El middleware borra la cookie al salir: una sesion que no sirve no
        # deberia quedar en el navegador provocando el mismo error en cada click.
        setattr(request.state, SESSION_CLEAR_ATTR, True)

    wants_html = "text/html" in request.headers.get("accept", "") and not is_htmx(request)
    if wants_html:
        target = request.url.path
        query = f"?next={target}" if target not in ("/", "/login") else ""
        return RedirectResponse(f"/login{query}", status_code=303)

    response: Response = JSONResponse({"detail": "no autenticado"}, status_code=401)
    if is_htmx(request):
        response.headers["HX-Redirect"] = "/login"
    return response


@app.exception_handler(NotAuthorizedError)
async def handle_not_authorized(request: Request, exc: NotAuthorizedError) -> Response:
    if "text/html" in request.headers.get("accept", "") and not is_htmx(request):
        return render(
            request,
            "error.html",
            {
                "title": "No tenés permiso",
                "detail": "Esta pantalla es solo para administradores.",
            },
            status_code=403,
        )
    return JSONResponse({"detail": exc.reason}, status_code=403)


@app.exception_handler(CSRFError)
async def handle_csrf_error(request: Request, exc: CSRFError) -> Response:
    """Un CSRF fallido casi siempre es una pestaña vieja, no un ataque.

    Igual se responde 403 y se pide reintentar: aceptar el POST porque "seguro es
    una pestaña vieja" seria exactamente el agujero que el token evita.
    """
    log.warning("token CSRF rechazado", path=request.url.path, reason=str(exc))
    if "text/html" in request.headers.get("accept", "") and not is_htmx(request):
        return render(
            request,
            "error.html",
            {
                "title": "El formulario venció",
                "detail": "Recargá la página y probá de nuevo.",
            },
            status_code=403,
        )
    return JSONResponse({"detail": "token CSRF invalido"}, status_code=403)


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
