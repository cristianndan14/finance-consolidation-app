"""Lectura de `app.categories`.

Las categorias del sistema (`user_id is null`) las carga el seed y no se editan
desde la aplicacion: son el vocabulario con el que el resto del proyecto habla.
Un usuario puede crear las suyas encima, con el mismo slug incluso, porque el
unique agrupa las del sistema bajo un UUID nulo sintetico.

RLS deja ver las propias; las del sistema tienen su propia politica de lectura.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT = """
select
    id::text        as id,
    user_id::text   as user_id,
    slug,
    name,
    kind,
    icon,
    color,
    sort_order
  from app.categories
"""

_ACTIVE = text(_SELECT + " where is_active order by sort_order, name")
_BY_SLUG = text(_SELECT + " where slug = any(cast(:slugs as text[])) and is_active")


@dataclass(frozen=True)
class Category:
    id: str
    user_id: str | None
    slug: str
    name: str
    kind: str
    icon: str | None
    color: str | None
    sort_order: int

    @property
    def is_system(self) -> bool:
        return self.user_id is None

    @property
    def is_spending(self) -> bool:
        """Si cuenta como consumo en el dashboard.

        Impuestos, intereses, comisiones y pagos del resumen NO son consumo. En
        un resumen argentino son cerca de un tercio de las lineas, y mezclarlos
        con el gasto hace que el total del mes no se parezca a nada.
        """
        return self.kind == "expense"


async def list_active(conn: AsyncConnection) -> list[Category]:
    rows = (await conn.execute(_ACTIVE)).mappings()
    return [Category(**row) for row in rows]


async def by_slug(conn: AsyncConnection, slugs: list[str]) -> dict[str, Category]:
    """Las categorias de esos slugs, indexadas por slug.

    Si un slug existe como categoria del sistema y tambien del usuario, gana la
    del usuario: es una personalizacion deliberada.
    """
    if not slugs:
        return {}

    rows = (await conn.execute(_BY_SLUG, {"slugs": slugs})).mappings()
    found: dict[str, Category] = {}
    for row in rows:
        category = Category(**row)
        existing = found.get(category.slug)
        if existing is None or (existing.is_system and not category.is_system):
            found[category.slug] = category
    return found
