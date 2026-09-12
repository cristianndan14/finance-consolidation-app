"""Proteccion CSRF para los formularios.

La sesion vive en una cookie, asi que el navegador la manda sola en cualquier
request al dominio — incluida una disparada por un formulario en otro sitio.
`SameSite=Lax` ya bloquea el caso clasico (un POST cross-site no lleva la cookie),
pero se apoya en el navegador: es una defensa, no la defensa.

El token es la segunda. Va en un campo oculto del formulario, esta firmado con el
`SESSION_SECRET` y **lleva adentro el id del usuario**. Eso es lo que hace que no
sirva reusar el token de otra sesion: un token valido para el usuario A falla al
verificarse contra la sesion de B. Y como no viaja en una cookie, un sitio ajeno
no tiene de donde sacarlo.

Solo se exige en los metodos que mutan estado. Un GET que necesite CSRF es un GET
que no deberia mutar nada.
"""

from __future__ import annotations

from typing import Final

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.settings import Settings, get_settings

CSRF_SALT: Final = "fc-csrf-v1"
CSRF_FIELD: Final = "csrf_token"

# Un formulario abierto y dejado en una pestaña por horas deberia seguir andando;
# uno de hace dias no. Es mas corto que la sesion a proposito.
CSRF_MAX_AGE_SECONDS: Final = 60 * 60 * 12


class CSRFError(Exception):
    """El token no vino, no es valido, o es de otra sesion."""


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    secret = settings.session_secret.get_secret_value()
    if not secret:
        raise CSRFError("SESSION_SECRET esta vacia: no se puede firmar el token CSRF")
    return URLSafeTimedSerializer(secret, salt=CSRF_SALT)


def issue(user_id: str, settings: Settings | None = None) -> str:
    """Token para los formularios que se le muestran a este usuario."""
    cfg = settings or get_settings()
    return _serializer(cfg).dumps(user_id)


def verify(token: str | None, user_id: str, settings: Settings | None = None) -> None:
    """Levanta `CSRFError` si el token no corresponde a la sesion actual."""
    if not token:
        raise CSRFError("falta el token CSRF")

    cfg = settings or get_settings()
    try:
        signed_for = _serializer(cfg).loads(token, max_age=CSRF_MAX_AGE_SECONDS)
    except BadSignature as exc:
        raise CSRFError("token CSRF invalido o vencido") from exc

    if signed_for != user_id:
        raise CSRFError("el token CSRF es de otra sesion")
