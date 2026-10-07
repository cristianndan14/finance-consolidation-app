"""Entorno de Jinja2 y helpers de render.

Un solo lugar arma el entorno para que el autoescape (que Jinja2Templates activa
para .html) no dependa de donde se instancie. Todo lo que se renderiza lleva
datos financieros o emails, asi que escapar por default no es opcional.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.domain.money import format_ars

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Los montos se muestran en es-AR (`1.234,56`) en todas las pantallas. Va como
# filtro y no como str() en cada contexto para que no haya dos formatos de
# numero conviviendo: en una pantalla cuya razon de ser es verificar cifras,
# leer `1234.56` en un lado y `1.234,56` en otro es una fuente de errores.
templates.env.filters["ars"] = format_ars

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


def compute_static_version(static_dir: Path) -> str:
    """Hash corto del contenido de los CSS, para cache-bust en `?v=`.

    `__version__` es fijo y el mtime lo pisa el COPY de Docker, asi que ninguno
    cambia de forma confiable entre deploys. Si falta la carpeta devuelve "dev":
    el import nunca falla (el error fuerte lo da el mount de StaticFiles).
    """
    if not static_dir.is_dir():
        return "dev"
    files = [static_dir / "app.css", *sorted((static_dir / "css").glob("*.css"))]
    return hash_contents(f.read_bytes() for f in files if f.is_file())


def hash_contents(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()[:10]


templates.env.globals["static_version"] = compute_static_version(STATIC_DIR)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name=name, context=context or {}, status_code=status_code
    )


def is_htmx(request: Request) -> bool:
    """HTMX pide fragmentos: una redireccion a la pagina de login se le
    inyectaria dentro del div. Los handlers de error lo consultan."""
    return request.headers.get("HX-Request") == "true"
