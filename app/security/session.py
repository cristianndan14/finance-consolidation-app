"""Sesion del navegador: cookie httpOnly firmada.

# Por que la sesion vive en una cookie y no en la base

Los tokens de Supabase (access + refresh) tienen que estar en algun lado entre
requests. Las dos opciones son una tabla de sesiones o una cookie firmada. Para
2 a 5 usuarios, la tabla agrega un query por request y un estado mas que limpiar,
sin comprar nada: no hace falta invalidacion masiva ni ver sesiones activas.

# Por que httpOnly y no localStorage

El access token es la credencial que le dice a Postgres quien sos. En
`localStorage` lo lee cualquier script que llegue a la pagina, y basta un XSS
para exfiltrarlo. En una cookie `httpOnly` el JavaScript de la pagina no puede
leerlo ni siquiera si se ejecuta codigo ajeno. El precio es que hay que defender
CSRF a mano (ver `csrf.py`), que es un problema mas acotado.

`SameSite=Lax` corta el CSRF de navegacion cruzada; `Secure` se activa en
produccion (en `http://localhost` el navegador descartaria la cookie).

# El contenido va firmado, no encriptado

`itsdangerous` firma: cualquiera con la cookie puede leer el payload, nadie puede
modificarlo. Alcanza, porque el payload no tiene nada que el propio dueño de la
sesion no sepa ya — su email y sus tokens. Lo que hay que impedir es que se
fabrique una sesion con el `sub` de otro, y eso lo impide la firma. Como segunda
linea, el access token se verifica ademas contra la clave del proyecto en cada
request (`security/jwt.py`), asi que una cookie robada no puede inventar claims.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

# El salt aisla esta firma de la de CSRF: el mismo secreto, dos propositos, y un
# token de uno no vale para el otro. La version permite invalidar todo cambiando
# el salt si el formato del payload cambia.
SESSION_SALT: Final = "fc-session-v1"


class SessionSecretMissingError(RuntimeError):
    """No hay `SESSION_SECRET`: firmar con un secreto vacio no es firmar."""


@dataclass(frozen=True)
class Session:
    """Lo minimo para reconstruir la identidad en la proxima request."""

    user_id: str
    email: str
    access_token: str
    refresh_token: str
    # `exp` del access token, en epoch. Se guarda para saber si hay que refrescar
    # sin tener que decodificar el token en cada request.
    access_expires_at: int

    def to_payload(self) -> dict[str, Any]:
        # Claves cortas: la cookie lleva dos JWT y el limite del navegador son
        # 4 KB. Los nombres largos no aportan aca.
        return {
            "u": self.user_id,
            "e": self.email,
            "at": self.access_token,
            "rt": self.refresh_token,
            "x": self.access_expires_at,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> Session | None:
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                user_id=str(payload["u"]),
                email=str(payload["e"]),
                access_token=str(payload["at"]),
                refresh_token=str(payload["rt"]),
                access_expires_at=int(payload["x"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def needs_refresh(self, margin_seconds: int) -> bool:
        return self.access_expires_at - margin_seconds <= int(time.time())


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    secret = settings.session_secret.get_secret_value()
    if not secret:
        raise SessionSecretMissingError(
            "SESSION_SECRET esta vacia: la cookie de sesion quedaria sin firma real. "
            'Generar con: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT)


def encode(session: Session, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    return _serializer(cfg).dumps(session.to_payload())


def decode(raw: str, settings: Settings | None = None) -> Session | None:
    """Devuelve la sesion, o `None` si la cookie es vieja, adulterada o rara.

    No levanta: una cookie invalida no es un error del servidor, es un usuario
    que tiene que volver a loguearse.
    """
    cfg = settings or get_settings()
    try:
        payload = _serializer(cfg).loads(raw, max_age=cfg.session_max_age_seconds)
    except SignatureExpired:
        log.info("cookie de sesion expirada")
        return None
    except BadSignature:
        # Firma invalida no es lo mismo que expirada: o el secreto rotó, o alguien
        # esta probando. Se registra distinto para que se pueda ver en el log.
        log.warning("cookie de sesion con firma invalida")
        return None
    return Session.from_payload(payload)


def attach(response: Response, session: Session, settings: Settings | None = None) -> None:
    """Escribe la cookie de sesion en la respuesta."""
    cfg = settings or get_settings()
    response.set_cookie(
        cfg.session_cookie_name,
        encode(session, cfg),
        max_age=cfg.session_max_age_seconds,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear(response: Response, settings: Settings | None = None) -> None:
    """Borra la cookie. Los mismos atributos que al setearla, o el navegador
    deja la vieja viva."""
    cfg = settings or get_settings()
    response.delete_cookie(
        cfg.session_cookie_name,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
    )
