"""Comercios normalizados y su memoria de alias.

# Las dos tablas y por que son dos

`merchants` es el comercio: "Spotify". `merchant_aliases` es como lo escribe cada
resumen: `SPOTIFY`, `MERPAGO SPOTIFY`, `SPOTIFY AB`. Un comercio tiene muchos
alias, y el alias es lo que se busca cuando llega una transaccion nueva.

La separacion es lo que hace barato el enriquecimiento: la primera vez que
aparece una descripcion nueva la resuelve el LLM y queda guardada; de ahi en
adelante sale de un indice. Con el uso, el costo tiende a cero.

# `source` no es decorativo

Un alias puede venir del modelo (`llm`), de una correccion del usuario (`user`) o
del seed. Cuando el usuario corrige, su version **pisa** a la del modelo y no se
vuelve a preguntar: es la señal mas confiable que el sistema tiene, y desperdiciarla
seria volver a equivocarse en el mismo comercio todos los meses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT = """
select
    id::text                  as id,
    slug,
    canonical_name,
    default_category_id::text as default_category_id,
    website,
    created_at,
    updated_at
  from app.merchants
"""

_BY_SLUG = text(_SELECT + " where slug = :slug")
_LIST = text(_SELECT + " order by canonical_name")

_UPSERT = text(
    """
    insert into app.merchants (slug, canonical_name, default_category_id)
    values (:slug, :canonical_name, :default_category_id)
    on conflict (user_id, slug) do update
       set canonical_name = excluded.canonical_name,
           default_category_id = coalesce(
             excluded.default_category_id, app.merchants.default_category_id
           )
    returning
    id::text                  as id,
    slug,
    canonical_name,
    default_category_id::text as default_category_id,
    website,
    created_at,
    updated_at
    """
)

_ALIASES = text(
    """
    select a.raw_key,
           a.merchant_id::text as merchant_id,
           a.source,
           a.confidence,
           m.canonical_name,
           m.default_category_id::text as default_category_id
      from app.merchant_aliases a
      join app.merchants m on m.id = a.merchant_id and m.user_id = a.user_id
     where a.raw_key = any(cast(:keys as text[]))
    """
)

# `hits` cuenta cuantas veces sirvio el alias: es lo que permite saber si el
# cache esta funcionando sin tener que instrumentar nada mas.
_TOUCH = text(
    """
    update app.merchant_aliases
       set hits = hits + 1
     where raw_key = any(cast(:keys as text[]))
    """
)

_UPSERT_ALIAS = text(
    """
    insert into app.merchant_aliases (merchant_id, raw_key, source, confidence)
    values (:merchant_id, :raw_key, :source, :confidence)
    on conflict (user_id, raw_key) do update
       set merchant_id = excluded.merchant_id,
           source      = excluded.source,
           confidence  = excluded.confidence
     -- Lo que corrigio una persona no lo pisa el modelo. Es la señal mas
     -- confiable que hay: si el usuario dijo que esto es Spotify, es Spotify.
     where app.merchant_aliases.source <> 'user' or excluded.source = 'user'
    """
)


@dataclass(frozen=True)
class Merchant:
    id: str
    slug: str
    canonical_name: str
    default_category_id: str | None
    website: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AliasHit:
    """Un alias que ya estaba en la memoria: no hay que preguntarle al modelo."""

    raw_key: str
    merchant_id: str
    canonical_name: str
    default_category_id: str | None
    source: str

    @property
    def from_user(self) -> bool:
        return self.source == "user"


async def list_all(conn: AsyncConnection) -> list[Merchant]:
    rows = (await conn.execute(_LIST)).mappings()
    return [Merchant(**row) for row in rows]


async def get_by_slug(conn: AsyncConnection, slug: str) -> Merchant | None:
    row = (await conn.execute(_BY_SLUG, {"slug": slug})).mappings().one_or_none()
    return Merchant(**row) if row else None


async def upsert(
    conn: AsyncConnection, *, slug: str, canonical_name: str, default_category_id: str | None
) -> Merchant:
    row = (
        (
            await conn.execute(
                _UPSERT,
                {
                    "slug": slug,
                    "canonical_name": canonical_name,
                    "default_category_id": default_category_id,
                },
            )
        )
        .mappings()
        .one()
    )
    return Merchant(**row)


async def lookup_aliases(conn: AsyncConnection, keys: list[str]) -> dict[str, AliasHit]:
    """Los alias que ya se conocen, indexados por clave.

    Es la consulta que decide cuanto se le paga al modelo: lo que sale de aca no
    se pregunta.
    """
    if not keys:
        return {}

    rows = (await conn.execute(_ALIASES, {"keys": keys})).mappings()
    return {
        row["raw_key"]: AliasHit(
            raw_key=row["raw_key"],
            merchant_id=row["merchant_id"],
            canonical_name=row["canonical_name"],
            default_category_id=row["default_category_id"],
            source=row["source"],
        )
        for row in rows
    }


async def touch_aliases(conn: AsyncConnection, keys: list[str]) -> None:
    if keys:
        await conn.execute(_TOUCH, {"keys": keys})


async def remember_alias(
    conn: AsyncConnection,
    *,
    raw_key: str,
    merchant_id: str,
    source: str,
    confidence: float | None = None,
) -> None:
    await conn.execute(
        _UPSERT_ALIAS,
        {
            "raw_key": raw_key,
            "merchant_id": merchant_id,
            "source": source,
            "confidence": confidence,
        },
    )
