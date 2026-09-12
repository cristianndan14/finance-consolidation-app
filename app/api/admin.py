"""Invitaciones: la unica via de alta de usuarios.

# Por que este modulo puede usar la service_role key

Crear un usuario en `auth.users` es lo unico que el rol `app_runtime` no puede
hacer: `auth` no es su schema. Por eso `app/infra/supabase_admin.py` esta en la
lista blanca de `scripts/check_service_role.py` y este archivo es uno de los
autorizados a importarlo. La key se usa **solo** para el alta en Auth; la fila de
`app.invitations` se escribe con la conexion del usuario admin y RLS activo, como
cualquier otro dato.

# Orden de las dos operaciones

Primero la fila, despues el mail. Al revés, si el insert fallara ya habria un
usuario creado en Auth sin registro de quien lo invito. Con este orden, si el mail
falla se borra la fila y el estado queda como antes.

# Que significa `accepted_at`

Lo pone el trigger `app.handle_new_auth_user` cuando aparece la fila en
`auth.users`, y GoTrue la crea al mandar la invitacion. Asi que `accepted_at` es
"la cuenta existe", no "la persona ya eligio contraseña" — eso ultimo vive en
`auth.users.confirmed_at`, que el backend no puede leer con `app_runtime`. La UI
lo dice con esas palabras para no prometer lo que no sabe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from app.deps import AdminDep, CurrentUserDep, SettingsDep
from app.infra import db, supabase_admin
from app.logging_config import get_logger
from app.repositories import invitations as invitations_repo
from app.security import csrf
from app.web.templates import render

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

INVITATIONS_PATH = "/admin/invitations"

# Una invitacion que nadie uso en una semana probablemente no se va a usar.
INVITATION_TTL_DAYS: Final = 7


async def _render_list(
    request: Request,
    user: CurrentUserDep,
    admin: AdminDep,
    settings: SettingsDep,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    async with db.user_tx(user.db_claims) as conn:
        pending = await invitations_repo.list_all(conn)

    return render(
        request,
        "admin/invitations.html",
        {
            "profile": admin,
            "invitations": pending,
            "csrf_token": csrf.issue(user.user_id, settings),
            "error": error,
            "notice": notice,
        },
        status_code=status_code,
    )


@router.get("/invitations", include_in_schema=False)
async def list_invitations(
    request: Request, user: CurrentUserDep, admin: AdminDep, settings: SettingsDep
) -> Response:
    return await _render_list(request, user, admin, settings)


@router.post("/invitations", include_in_schema=False)
async def create_invitation(
    request: Request,
    user: CurrentUserDep,
    admin: AdminDep,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    csrf.verify(csrf_token, user.user_id, settings)

    address = email.strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        return await _render_list(
            request, user, admin, settings, error="Eso no parece un email.", status_code=400
        )

    expires_at = datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS)

    async with db.user_tx(user.db_claims) as conn:
        invitation = await invitations_repo.create(conn, address, expires_at)

    if invitation is None:
        return await _render_list(
            request,
            user,
            admin,
            settings,
            error="Ese email ya tiene una invitacion aceptada.",
            status_code=409,
        )

    try:
        await supabase_admin.invite_user_by_email(address, settings)
    except supabase_admin.AdminError as exc:
        # Sin mail, la fila no representa nada: se deshace para que reintentar
        # desde cero sea posible.
        async with db.user_tx(user.db_claims) as conn:
            await invitations_repo.revoke(conn, invitation.id)
        return await _render_list(request, user, admin, settings, error=str(exc), status_code=502)

    log.info("invitacion enviada", invitation_id=invitation.id)
    return RedirectResponse(f"{INVITATIONS_PATH}?sent=1", status_code=303)


@router.post("/invitations/{invitation_id}/revoke", include_in_schema=False)
async def revoke_invitation(
    request: Request,
    user: CurrentUserDep,
    admin: AdminDep,
    settings: SettingsDep,
    invitation_id: str,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Borra una invitacion que todavia no genero cuenta.

    No toca `auth.users`: dar de baja a alguien que ya tiene cuenta es otra
    operacion, con otras consecuencias (se lleva todos sus datos por cascade).
    """
    csrf.verify(csrf_token, user.user_id, settings)

    async with db.user_tx(user.db_claims) as conn:
        removed = await invitations_repo.revoke(conn, invitation_id)

    if not removed:
        return await _render_list(
            request,
            user,
            admin,
            settings,
            error="Esa invitacion ya no esta pendiente.",
            status_code=409,
        )

    log.info("invitacion revocada", invitation_id=invitation_id)
    return RedirectResponse(INVITATIONS_PATH, status_code=303)
