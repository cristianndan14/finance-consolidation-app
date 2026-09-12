"""Cliente de GoTrue (Supabase Auth) con la anon key.

Solo autenticacion: login, refresh y logout. **Los datos de negocio nunca pasan
por aca** — van por `app/infra/db.py` con el rol `app_runtime` y RLS activo. Esa
separacion es la que permite que la anon key este en este modulo sin ampliar la
superficie: con la anon key sola no se lee ni una fila de `app`.

Se usa `httpx` contra el REST de GoTrue en lugar del SDK `supabase-py` porque el
SDK trae su propio manejo de sesion y storage, que aca no hace falta: la sesion
la maneja `security/session.py`. Tres endpoints y una respuesta JSON es menos
codigo del que costaria adaptar el SDK.

Los mensajes de error que salen de este modulo son deliberadamente vagos hacia el
usuario ("credenciales invalidas"), para no distinguir "el email no existe" de
"la contraseña esta mal": eso convertiria el login en un enumerador de usuarios.
El detalle real va al log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

AUTH_TIMEOUT_SECONDS: Final = 10.0

# Fallback si GoTrue no manda `expires_at` ni `expires_in`.
DEFAULT_TOKEN_TTL_SECONDS: Final = 3600


class AuthError(Exception):
    """Fallo de autenticacion. `message` es lo unico que se le muestra al usuario."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TokenPair:
    """Lo que devuelve GoTrue cuando la autenticacion sale bien."""

    access_token: str
    refresh_token: str
    expires_at: int
    user_id: str
    email: str


def _auth_url(settings: Settings) -> str:
    return f"{settings.supabase_url.rstrip('/')}/auth/v1"


def _headers(settings: Settings) -> dict[str, str]:
    key = settings.supabase_anon_key.get_secret_value()
    if not key:
        raise AuthError("el servicio de autenticacion no esta configurado")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _parse_tokens(payload: dict[str, Any]) -> TokenPair:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    user = payload.get("user") or {}
    user_id = user.get("id")

    if not access_token or not refresh_token or not user_id:
        raise AuthError("respuesta inesperada del servicio de autenticacion")

    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, int):
        expires_in = payload.get("expires_in")
        ttl = expires_in if isinstance(expires_in, int) else DEFAULT_TOKEN_TTL_SECONDS
        expires_at = int(time.time()) + ttl

    return TokenPair(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_at=expires_at,
        user_id=str(user_id),
        email=str(user.get("email") or ""),
    )


async def _post(
    path: str,
    *,
    settings: Settings,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    bearer: str | None = None,
) -> httpx.Response:
    headers = _headers(settings)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        async with httpx.AsyncClient(timeout=AUTH_TIMEOUT_SECONDS) as client:
            return await client.post(
                f"{_auth_url(settings)}{path}", headers=headers, json=json, params=params
            )
    except httpx.HTTPError as exc:
        log.error("no se pudo contactar a Supabase Auth", path=path, error=type(exc).__name__)
        raise AuthError("el servicio de autenticacion no responde") from exc


def _gotrue_error(response: httpx.Response) -> str:
    """Extrae el motivo del fallo para el log. Nunca se le muestra al usuario."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("msg") or body.get("error") or body)
    return str(body)[:200]


async def sign_in_with_password(
    email: str, password: str, settings: Settings | None = None
) -> TokenPair:
    """Login con email y contraseña."""
    cfg = settings or get_settings()
    response = await _post(
        "/token",
        settings=cfg,
        params={"grant_type": "password"},
        json={"email": email, "password": password},
    )

    if response.status_code == 400:
        log.info("login rechazado", reason=_gotrue_error(response))
        raise AuthError("email o contraseña incorrectos", status=400)
    if response.status_code == 429:
        raise AuthError("demasiados intentos; probar de nuevo en unos minutos", status=429)
    if response.status_code >= 400:
        log.error(
            "Supabase Auth respondio con error",
            status=response.status_code,
            reason=_gotrue_error(response),
        )
        raise AuthError("no se pudo iniciar sesion", status=response.status_code)

    return _parse_tokens(response.json())


async def refresh_session(refresh_token: str, settings: Settings | None = None) -> TokenPair:
    """Canjea el refresh token por un access token nuevo.

    Si falla, la sesion esta muerta (el refresh token se rotó o se revocó) y el
    llamador tiene que mandar al login: reintentar no sirve.
    """
    cfg = settings or get_settings()
    response = await _post(
        "/token",
        settings=cfg,
        params={"grant_type": "refresh_token"},
        json={"refresh_token": refresh_token},
    )

    if response.status_code >= 400:
        log.info("refresh de sesion rechazado", status=response.status_code)
        raise AuthError("la sesion expiro", status=response.status_code)

    return _parse_tokens(response.json())


async def sign_out(access_token: str, settings: Settings | None = None) -> None:
    """Revoca la sesion del lado de Supabase.

    El logout no depende de que esto funcione: la cookie se borra igual. Si la
    llamada falla se registra y sigue, porque dejar la cookie viva seria peor.
    """
    cfg = settings or get_settings()
    try:
        response = await _post("/logout", settings=cfg, bearer=access_token)
    except AuthError as exc:
        log.warning("no se pudo revocar la sesion en Supabase", error=exc.message)
        return

    if response.status_code >= 400:
        log.warning("Supabase Auth rechazo el logout", status=response.status_code)
