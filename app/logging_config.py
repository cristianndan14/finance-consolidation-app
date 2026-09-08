"""Logging estructurado con redaccion de datos sensibles.

El contenido que maneja esta app es, literalmente, la lista completa de consumos de
una persona con su nombre y los ultimos digitos de sus tarjetas. Un log de debug
descuidado la deja escrita en el disco de Fly.io o en la consola de un tercero.

La politica es de lista negra por clave mas truncado por largo:

- Las claves de `REDACT_KEYS` nunca se emiten: se reemplazan por `<redacted:N>`,
  que conserva el largo (util para debuggear) y no el contenido.
- Cualquier string mas largo que `MAX_STR` se trunca, porque un payload grande casi
  siempre es texto de un resumen.
- `raw_response` del LLM y `full_text` del PDF viven en la base de datos, que es
  donde tienen que estar. Nunca en un log.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# Claves cuyo valor no se emite nunca.
REDACT_KEYS: frozenset[str] = frozenset(
    {
        # contenido de resumenes
        "full_text",
        "page_texts",
        "text_layout",
        "text_flow",
        "raw_response",
        "description_raw",
        "source_line",
        "text_chunk",
        "text",
        "tables",
        # identidad y credenciales
        "pdf_password",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "gemini_api_key",
        "session_secret",
        "anon_key",
        "cookie",
        "set-cookie",
        "jwt",
        "claims",
        "email",
        "last4",
        "card_number",
        "cuit",
        "dni",
    }
)

MAX_STR = 200


def _redact(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    return {key: _scrub(key, value) for key, value in event_dict.items()}


def _scrub(key: str, value: Any, depth: int = 0) -> Any:
    if key.lower() in REDACT_KEYS:
        length = len(value) if isinstance(value, str | bytes | list | dict) else "?"
        return f"<redacted:{length}>"

    # Cortar recursion en estructuras profundas o ciclicas.
    if depth > 4:
        return "<deep>"

    if isinstance(value, dict):
        return {k: _scrub(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        if len(value) > 20:
            return [_scrub(key, v, depth + 1) for v in value[:20]] + [f"<+{len(value) - 20} mas>"]
        return [_scrub(key, v, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + f"...<+{len(value) - MAX_STR} chars>"
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    """Configura structlog y el logging stdlib para que compartan la salida."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact,  # ← despues de armar el evento, antes de renderizar
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Los loggers de librerias (uvicorn, sqlalchemy, httpx) pasan por el mismo formato.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # asyncpg loguea las queries con los parametros: apagado.
    for noisy in ("sqlalchemy.engine", "asyncpg", "httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
