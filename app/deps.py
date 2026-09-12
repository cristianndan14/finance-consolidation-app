"""Dependencias de FastAPI: de una cookie a una identidad verificada.

El camino de cada request autenticada es siempre este:

    cookie firmada  ->  session  ->  access token verificado  ->  claims
                                                                    |
                            app.infra.db.user_tx(claims)  ->  Postgres con RLS

Dos verificaciones seguidas, a proposito. La firma de la cookie prueba que la
sesion la emitio este backend; la verificacion del JWT prueba que los claims los
emitio Supabase. Si solo se firmara la cookie, un `SESSION_SECRET` filtrado
permitiria fabricar cualquier `sub`. Si solo se verificara el JWT, habria que
guardarlo en un lugar accesible por JavaScript.

El refresh del access token pasa aca porque es el unico lugar que ve la sesion
completa. La cookie nueva no se escribe en la dependencia (no tiene la response a
mano): se deja en `request.state` y la escribe `SessionCookieMiddleware`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from app.infra import db, supabase_auth
from app.logging_config import get_logger
from app.repositories import profiles as profiles_repo
from app.security import jwt as jwt_verify
from app.security import session as session_store
from app.security.exceptions import NotAuthenticatedError, NotAuthorizedError
from app.settings import Settings, get_settings

log = get_logger(__name__)

# Nombres de los atributos que la dependencia deja para el middleware.
SESSION_UPDATE_ATTR = "session_update"
SESSION_CLEAR_ATTR = "session_clear"


@dataclass(frozen=True)
class CurrentUser:
    """Identidad verificada de la request en curso."""

    user_id: str
    email: str
    claims: dict[str, Any]
    # El access token de Supabase. Lo necesita Storage, que aplica sus politicas
    # con el JWT del usuario y no con la conexion de Postgres.
    access_token: str

    @property
    def db_claims(self) -> dict[str, Any]:
        """Lo que se le pasa a `user_tx`. El filtrado de claims lo hace `db.py`:
        aca no se decide que es seguro propagar."""
        return self.claims


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def optional_session(request: Request, settings: SettingsDep) -> session_store.Session | None:
    """La sesion de la cookie, si hay una valida. Para paginas publicas."""
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    return session_store.decode(raw, settings)


OptionalSession = Annotated[session_store.Session | None, Depends(optional_session)]


async def current_user(
    request: Request, session: OptionalSession, settings: SettingsDep
) -> CurrentUser:
    """Exige una sesion valida y devuelve la identidad ya verificada."""
    if session is None:
        raise NotAuthenticatedError("no hay sesion")

    if session.needs_refresh(settings.session_refresh_margin_seconds):
        session = await _refresh(request, session, settings)

    try:
        token = await jwt_verify.verify_token(session.access_token, settings)
    except jwt_verify.TokenError as exc:
        # La cookie esta bien firmada pero el token que lleva no sirve. Puede ser
        # una rotacion de claves del proyecto o un token de otro proyecto: en
        # cualquier caso, volver a loguearse es lo unico que arregla.
        log.info("access token rechazado", reason=str(exc))
        raise NotAuthenticatedError("el token no es valido", clear_cookie=True) from exc

    if token.user_id != session.user_id:
        # No deberia pasar nunca. Si pasa, algo esta mezclando sesiones y la
        # respuesta correcta es cortar, no elegir uno de los dos ids.
        log.error("la cookie y el token no coinciden en el usuario")
        raise NotAuthenticatedError("sesion inconsistente", clear_cookie=True)

    return CurrentUser(
        user_id=token.user_id,
        email=token.email or session.email,
        claims=token.claims,
        access_token=session.access_token,
    )


async def _refresh(
    request: Request, session: session_store.Session, settings: Settings
) -> session_store.Session:
    try:
        tokens = await supabase_auth.refresh_session(session.refresh_token, settings)
    except supabase_auth.AuthError as exc:
        raise NotAuthenticatedError(exc.message, clear_cookie=True) from exc

    renewed = session_store.Session(
        user_id=tokens.user_id,
        email=tokens.email or session.email,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.expires_at,
    )
    # Supabase rota el refresh token en cada uso: si la cookie nueva no se
    # escribe, la vieja queda con un refresh token ya consumido y la proxima
    # request tira al usuario al login.
    setattr(request.state, SESSION_UPDATE_ATTR, renewed)
    return renewed


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


async def current_profile(user: CurrentUserDep) -> profiles_repo.Profile:
    """El perfil del usuario, leido con RLS activo.

    Si no existe, es que el trigger `handle_new_auth_user` no corrio: la cuenta
    existe en `auth.users` pero no en `app.profiles`. Es un estado invalido, no
    un usuario nuevo, y conviene que se vea.
    """
    async with db.user_tx(user.db_claims) as conn:
        profile = await profiles_repo.get_current(conn)

    if profile is None:
        log.error("usuario autenticado sin perfil en app.profiles", user_id=user.user_id)
        raise NotAuthenticatedError("la cuenta no tiene perfil", clear_cookie=True)
    return profile


CurrentProfileDep = Annotated[profiles_repo.Profile, Depends(current_profile)]


async def require_admin(profile: CurrentProfileDep) -> profiles_repo.Profile:
    """Para las pantallas de administracion.

    El rol se lee de `app.profiles`, no del JWT: los claims de metadata los puede
    editar el propio usuario desde el API de Auth, asi que un `role` que venga en
    el token no es una autorizacion.
    """
    if not profile.is_admin:
        raise NotAuthorizedError("hace falta ser admin")
    return profile


AdminDep = Annotated[profiles_repo.Profile, Depends(require_admin)]
