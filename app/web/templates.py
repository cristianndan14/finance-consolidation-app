"""Entorno de Jinja2 y helpers de render.

Un solo lugar arma el entorno para que el autoescape (que Jinja2Templates activa
para .html) no dependa de donde se instancie. Todo lo que se renderiza lleva
datos financieros o emails, asi que escapar por default no es opcional.
"""

from __future__ import annotations

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
