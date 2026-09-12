"""Lectura y escritura de `app.invitations`.

Solo un admin llega a estas consultas — no por un `if` en Python, sino porque la
politica `invitations_admin_all` lee `app.profiles.role` del usuario de la
transaccion. Si un no-admin ejecutara estas queries veria cero filas y sus
inserts serian rechazados por el `with check`.

`invited_by` se llena con `auth.uid()` en el SQL, no con un parametro: asi no hay
forma de registrar una invitacion a nombre de otro, ni por un bug de la capa de
arriba.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_LIST = text(
    """
    select i.id::text        as id,
           i.email           as email,
           i.expires_at      as expires_at,
           i.accepted_at     as accepted_at,
           i.created_at      as created_at,
           p.email           as invited_by_email
      from app.invitations i
      join app.profiles p on p.id = i.invited_by
     order by i.created_at desc
    """
)

# `on conflict (email)` cubre el caso de reinvitar a alguien que nunca acepto:
# extiende el vencimiento en lugar de fallar con una violacion de unicidad.
_CREATE = text(
    """
    insert into app.invitations (email, invited_by, expires_at)
    values (:email, auth.uid(), :expires_at)
    on conflict (email) do update
       set expires_at = excluded.expires_at,
           invited_by = excluded.invited_by
     where app.invitations.accepted_at is null
    returning id::text as id, email, expires_at, accepted_at, created_at
    """
)

_DELETE = text("delete from app.invitations where id = :id and accepted_at is null")


@dataclass(frozen=True)
class Invitation:
    id: str
    email: str
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime
    invited_by_email: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None


async def list_all(conn: AsyncConnection) -> list[Invitation]:
    rows = (await conn.execute(_LIST)).mappings().all()
    return [Invitation(**row) for row in rows]


async def create(conn: AsyncConnection, email: str, expires_at: datetime) -> Invitation | None:
    """Registra la invitacion. `None` si el email ya tiene una invitacion aceptada."""
    row = (
        (await conn.execute(_CREATE, {"email": email, "expires_at": expires_at}))
        .mappings()
        .one_or_none()
    )
    return Invitation(**row) if row else None


async def revoke(conn: AsyncConnection, invitation_id: str) -> bool:
    """Borra una invitacion pendiente. Una ya aceptada no se toca: para dar de
    baja a un usuario hay que borrarlo de `auth.users`."""
    result = await conn.execute(_DELETE, {"id": invitation_id})
    return (result.rowcount or 0) > 0
