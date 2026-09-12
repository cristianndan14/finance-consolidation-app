"""Lectura y escritura de `app.profiles`.

Como todo repositorio del proyecto, recibe la `conn` ya contextualizada por
`app.infra.db.user_tx()` y **no** abre conexiones. Eso es lo que hace imposible
que una consulta se saltee el contexto de RLS por accidente.

Ninguna consulta de aca filtra por `user_id` a mano: la politica `profiles_select`
ya limita a la propia fila. Un `select *` sin `where` devuelve exactamente un
perfil — el del usuario de la transaccion — o cero filas si no hay identidad.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT_ME = text(
    """
    select id::text as id, email, display_name, base_currency, role
      from app.profiles
     where id = auth.uid()
    """
)

_UPDATE_ME = text(
    """
    update app.profiles
       set display_name  = coalesce(:display_name, display_name),
           base_currency = coalesce(:base_currency, base_currency)
     where id = auth.uid()
    returning id::text as id, email, display_name, base_currency, role
    """
)


@dataclass(frozen=True)
class Profile:
    id: str
    email: str
    display_name: str | None
    base_currency: str
    role: str

    @property
    def is_admin(self) -> bool:
        """`admin` solo habilita invitar. No da acceso a datos de nadie: eso lo
        impide RLS, no este flag."""
        return self.role == "admin"


async def get_current(conn: AsyncConnection) -> Profile | None:
    """El perfil del usuario de la transaccion, o `None` si no existe todavia."""
    row = (await conn.execute(_SELECT_ME)).mappings().one_or_none()
    return Profile(**row) if row else None


async def update_current(
    conn: AsyncConnection,
    *,
    display_name: str | None = None,
    base_currency: str | None = None,
) -> Profile | None:
    """Actualiza preferencias. `role` no es editable desde aca a proposito: la
    politica `profiles_update` tampoco lo permitiria."""
    row = (
        (
            await conn.execute(
                _UPDATE_ME, {"display_name": display_name, "base_currency": base_currency}
            )
        )
        .mappings()
        .one_or_none()
    )
    return Profile(**row) if row else None
