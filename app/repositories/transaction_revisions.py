"""Bitacora de correcciones manuales: `app.transaction_revisions`.

# Para que se guarda

Podria parecer contabilidad de mas — el dato corregido ya esta en la fila. Sirve
para tres cosas concretas:

1. **Medir la accuracy real del modelo por emisor.** "El 12% de las lineas de
   Galicia hay que corregirlas" es un dato que solo existe si se registra.
2. **Alimentar ejemplos few-shot** (Fase 6) con las correcciones reales, que es
   la forma mas barata de mejorar la extraccion de un emisor especifico.
3. **Explicar por que un dato no coincide con el PDF.** Tres meses despues, la
   pregunta "¿esto lo leyo mal el modelo o lo cambie yo?" tiene respuesta.

# Por que los valores van como jsonb y no como texto

Los campos que se editan tienen tipos distintos: una fecha, un monto, un entero
de cuotas, un enum. Guardarlos como texto obligaria a parsear de vuelta para
comparar, y `'1000'` contra `'1000.00'` se veria como un cambio cuando no lo es.

Al leer **no** se hace `json.loads`: el driver ya decodifica `jsonb`. Hacerlo de
nuevo funciona de casualidad con objetos y arrays (que dejan de ser `str` al
decodificarse) y rompe con los escalares, que son la mayoria de los valores de
esta tabla: un `"charge"` decodificado es el str `charge`, y volver a parsearlo
es un `JSONDecodeError`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_INSERT = text(
    """
    insert into app.transaction_revisions (transaction_id, field, old_value, new_value)
    values (:transaction_id, :field, cast(:old_value as jsonb), cast(:new_value as jsonb))
    returning id::text as id
    """
)

_FOR_TRANSACTION = text(
    """
    select id::text as id, transaction_id::text as transaction_id,
           field, old_value, new_value, changed_at
      from app.transaction_revisions
     where transaction_id = :transaction_id
     order by changed_at desc
    """
)

_FOR_STATEMENT = text(
    """
    select r.id::text as id, r.transaction_id::text as transaction_id,
           r.field, r.old_value, r.new_value, r.changed_at
      from app.transaction_revisions r
      join app.transactions t on t.id = r.transaction_id
     where t.statement_id = :statement_id
     order by r.changed_at desc
     limit :limit
    """
)


@dataclass(frozen=True)
class Revision:
    id: str
    transaction_id: str
    field: str
    old_value: Any
    new_value: Any
    changed_at: datetime


@dataclass(frozen=True)
class FieldChange:
    """Un campo que cambio de valor. Lo arma el servicio de revision."""

    field: str
    old: Any
    new: Any


async def record(
    conn: AsyncConnection, *, transaction_id: str, changes: Sequence[FieldChange]
) -> int:
    """Registra los cambios de una edicion. Devuelve cuantos."""
    for change in changes:
        await conn.execute(
            _INSERT,
            {
                "transaction_id": transaction_id,
                "field": change.field,
                "old_value": json.dumps(change.old, default=str),
                "new_value": json.dumps(change.new, default=str),
            },
        )
    return len(changes)


async def for_transaction(conn: AsyncConnection, transaction_id: str) -> list[Revision]:
    rows = (await conn.execute(_FOR_TRANSACTION, {"transaction_id": transaction_id})).mappings()
    return [Revision(**row) for row in rows]


async def for_statement(
    conn: AsyncConnection, statement_id: str, *, limit: int = 200
) -> list[Revision]:
    rows = (
        await conn.execute(_FOR_STATEMENT, {"statement_id": statement_id, "limit": limit})
    ).mappings()
    return [Revision(**row) for row in rows]
