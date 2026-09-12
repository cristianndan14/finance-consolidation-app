"""Middleware que escribe la cookie de sesion cuando algo la renovo.

El refresh del access token ocurre en una dependencia (`app/deps.py`), que no
tiene la response a mano. En lugar de pasear la response por las dependencias,
la dependencia deja la sesion nueva en `request.state` y esto la escribe al
salir. El resultado es que ninguna ruta tiene que acordarse de renovar la cookie:
si se renovo el token, la cookie sale actualizada.

Importa que sea asi y no "que cada handler la setee": Supabase **rota** el refresh
token en cada uso, asi que una cookie que no se actualiza queda con un refresh
token ya consumido y la proxima request manda al usuario al login.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.deps import SESSION_CLEAR_ATTR, SESSION_UPDATE_ATTR
from app.security import session as session_store


class SessionCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)

        # Borrar gana sobre renovar: si algo decidio que la sesion no sirve, no
        # tiene sentido escribir una version nueva de una sesion invalida.
        if getattr(request.state, SESSION_CLEAR_ATTR, False):
            session_store.clear(response)
            return response

        renewed = getattr(request.state, SESSION_UPDATE_ATTR, None)
        if renewed is not None:
            session_store.attach(response, renewed)
        return response
