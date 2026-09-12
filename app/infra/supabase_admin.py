"""El unico modulo que puede usar la service_role key de Supabase.

# Por que esta acotado a un solo modulo

La service_role key se conecta con un rol que tiene `BYPASSRLS`: las politicas de
Row Level Security **no se evaluan**. Cualquier query hecha con ella ve la base
entera. Si esa key estuviera disponible en cualquier modulo, el aislamiento entre
usuarios dejaria de estar garantizado por Postgres y pasaria a depender de que
cada query recuerde su `where user_id`.

`scripts/check_service_role.py` corre en pre-commit y en CI, y falla si otro
modulo importa este o menciona la variable de entorno.

# Inventario cerrado de usos (plan, seccion 1)

1. **Invitaciones y alta de usuarios** — toca solo `auth.users`, via el API admin
   de GoTrue. Es lo que hay en este archivo hoy.
2. **Seed** de categorias del sistema y tipos de cambio globales — en CLI o
   migracion, nunca en un request.
3. **Reaper de jobs huerfanos** — solo `processing_jobs.status` (Fase 2).

Prohibido tocar `documents`, `document_texts`, `statements`, `transactions` o
`merchants`: eso va por `app/infra/db.py` con el JWT del usuario. Nada en este
modulo abre una conexion a Postgres — solo habla con el API de Auth por HTTP, que
es la unica cosa que el rol `app_runtime` no puede hacer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

ADMIN_TIMEOUT_SECONDS: Final = 15.0


class AdminError(Exception):
    """Fallo de una operacion administrativa contra Supabase Auth."""


@dataclass(frozen=True)
class InvitedUser:
    user_id: str
    email: str


def _admin_headers(settings: Settings) -> dict[str, str]:
    key = settings.supabase_service_role_key.get_secret_value()
    if not key:
        raise AdminError(
            "falta SUPABASE_SERVICE_ROLE_KEY: las invitaciones necesitan permisos de admin"
        )
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _admin_url(settings: Settings, path: str) -> str:
    return f"{settings.supabase_url.rstrip('/')}/auth/v1{path}"


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("error_description") or body.get("error") or body)
    return str(body)[:200]


async def invite_user_by_email(email: str, settings: Settings | None = None) -> InvitedUser:
    """Crea el usuario en `auth.users` y le manda el mail de invitacion.

    El alta del perfil en `app.profiles` no se hace aca: lo hace el trigger
    `app.handle_new_auth_user` dentro de la misma transaccion que el insert en
    `auth.users`. Hacerlo desde el backend dejaria usuarios sin perfil si la
    request se cortara en el medio.
    """
    cfg = settings or get_settings()
    payload: dict[str, Any] = {"email": email}

    try:
        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _admin_url(cfg, "/invite"),
                headers=_admin_headers(cfg),
                json=payload,
                # A donde cae el usuario despues de aceptar y elegir contraseña.
                params={"redirect_to": f"{cfg.base_url.rstrip('/')}/login"},
            )
    except httpx.HTTPError as exc:
        log.error("no se pudo invitar al usuario", error=type(exc).__name__)
        raise AdminError("el servicio de autenticacion no responde") from exc

    if response.status_code == 422:
        # GoTrue devuelve 422 cuando el email ya tiene usuario.
        raise AdminError("ese email ya tiene una cuenta")
    if response.status_code >= 400:
        log.error(
            "Supabase Auth rechazo la invitacion",
            status=response.status_code,
            reason=_reason(response),
        )
        raise AdminError("no se pudo enviar la invitacion")

    body = response.json()
    user_id = body.get("id") if isinstance(body, dict) else None
    if not user_id:
        raise AdminError("respuesta inesperada del servicio de autenticacion")

    log.info("usuario invitado", user_id=str(user_id))
    return InvitedUser(user_id=str(user_id), email=email)


async def delete_user(user_id: str, settings: Settings | None = None) -> None:
    """Borra el usuario de `auth.users`.

    El `on delete cascade` de `app.profiles` se lleva el perfil y, por la cadena
    de FKs, todos sus datos. Es la unica forma de dar de baja a alguien: no hay
    borrado parcial.
    """
    cfg = settings or get_settings()
    try:
        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as client:
            response = await client.delete(
                _admin_url(cfg, f"/admin/users/{user_id}"), headers=_admin_headers(cfg)
            )
    except httpx.HTTPError as exc:
        raise AdminError("el servicio de autenticacion no responde") from exc

    if response.status_code >= 400:
        log.error(
            "no se pudo borrar el usuario", status=response.status_code, reason=_reason(response)
        )
        raise AdminError("no se pudo borrar el usuario")

    log.info("usuario borrado", user_id=user_id)
