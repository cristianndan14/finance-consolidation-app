"""Fallos de autenticacion y autorizacion, como excepciones de dominio.

No se usa `HTTPException` porque la respuesta correcta depende de quien pregunta:
un navegador pidiendo una pagina tiene que ir al login (303), y una llamada de
HTMX o del API tiene que recibir 401 sin redirect, o el fragmento HTML del login
terminaria inyectado dentro de la pagina. Esa decision es de presentacion y vive
en los handlers de `app/main.py`; las dependencias solo dicen que paso.
"""

from __future__ import annotations


class NotAuthenticatedError(Exception):
    """No hay sesion utilizable. Si `clear_cookie`, la que habia ya no sirve."""

    def __init__(self, reason: str = "sesion no valida", *, clear_cookie: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.clear_cookie = clear_cookie


class NotAuthorizedError(Exception):
    """Hay sesion, pero no alcanza para esta accion."""

    def __init__(self, reason: str = "accion no permitida") -> None:
        super().__init__(reason)
        self.reason = reason
