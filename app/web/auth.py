"""Rutas de login y logout.

No hay registro: las cuentas se crean por invitacion desde `/admin/invitations`.
Esta pantalla solo canjea email + contraseña por una sesion.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from app.deps import SESSION_CLEAR_ATTR, CurrentUserDep, OptionalSession, SettingsDep
from app.infra import supabase_auth
from app.logging_config import get_logger
from app.security import csrf
from app.security import session as session_store
from app.web.templates import render

log = get_logger(__name__)

router = APIRouter(tags=["auth"])

LOGIN_PATH = "/login"
HOME_PATH = "/"


def safe_redirect(target: str | None) -> str:
    """Filtra el `?next=` para que no pueda mandar a otro sitio.

    Un `next` sin validar convierte el login en un redirector abierto: un link a
    `/login?next=https://sitio-falso/` que despues del login exitoso lleva a una
    pagina clonada es un phishing con la barra de direcciones correcta hasta el
    ultimo paso. Solo se aceptan rutas de este sitio.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return HOME_PATH
    # `//host` ya quedo afuera; esto ademas descarta `/\host` y las URLs con
    # esquema que algun navegador pudiera normalizar.
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or target.startswith("/\\"):
        return HOME_PATH
    return target


@router.get(LOGIN_PATH, include_in_schema=False)
async def login_form(request: Request, session: OptionalSession, next: str = "") -> Response:
    """Formulario de login. Con sesion viva, no tiene sentido mostrarlo."""
    if session is not None:
        return RedirectResponse(safe_redirect(next), status_code=303)
    return render(request, "login.html", {"next": safe_redirect(next), "error": None})


@router.post(LOGIN_PATH, include_in_schema=False)
async def login_submit(
    request: Request,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
) -> Response:
    """Canjea las credenciales por una sesion.

    Sin token CSRF: todavia no hay sesion a la que atarlo, y un CSRF de login
    ("loguearte a la fuerza en una cuenta ajena") no da acceso a nada de este
    usuario. `SameSite=Lax` en la cookie que se emite corta el resto.
    """
    destination = safe_redirect(next)

    try:
        tokens = await supabase_auth.sign_in_with_password(email.strip(), password, settings)
    except supabase_auth.AuthError as exc:
        # 200 y no 401: es un formulario con un error, no una API. El status 401
        # haria que el navegador ofreciera su propio dialogo de autenticacion.
        return render(
            request,
            "login.html",
            {"next": destination, "error": exc.message, "email": email},
            status_code=200,
        )

    session = session_store.Session(
        user_id=tokens.user_id,
        email=tokens.email or email.strip(),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.expires_at,
    )

    log.info("sesion iniciada", user_id=session.user_id)
    response = RedirectResponse(destination, status_code=303)
    session_store.attach(response, session, settings)
    return response


@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    session: OptionalSession,
    user: CurrentUserDep,
    settings: SettingsDep,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Cierra la sesion en Supabase y borra la cookie.

    Lleva CSRF igual que cualquier POST: un logout forzado desde otro sitio no
    roba datos, pero es una molestia gratuita que se evita con una linea.
    """
    csrf.verify(csrf_token, user.user_id, settings)

    if session is not None:
        await supabase_auth.sign_out(session.access_token, settings)

    # El borrado lo hace el middleware, no este handler: si la dependencia
    # renovo el token en el camino, dejo una cookie nueva pendiente de escribir,
    # y borrar aca la sobreescribiria despues. Marcarlo en `request.state` es lo
    # que hace que borrar gane sobre renovar.
    setattr(request.state, SESSION_CLEAR_ATTR, True)

    log.info("sesion cerrada", user_id=user.user_id)
    return RedirectResponse(LOGIN_PATH, status_code=303)
