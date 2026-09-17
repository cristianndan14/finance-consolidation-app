"""Movimientos manuales: `app.manual_entries`.

Ingresos y gastos que no vienen de un resumen de tarjeta (efectivo,
transferencias, sueldo). Se cargan directo por el usuario y no pasan por
revision: `app/domain/validation.py` no interviene aca, porque no hay nada que
un modelo pueda haber leido mal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT = """
select
    e.id::text          as id,
    e.user_id::text      as user_id,
    e.entry_date,
    e.kind,
    e.description,
    e.amount,
    e.currency,
    e.category_id::text as category_id,
    cat.name             as category_name,
    e.notes
  from app.manual_entries e
  left join app.categories cat on cat.id = e.category_id
"""

_LIST_RECENT = text(_SELECT + " order by e.entry_date desc, e.created_at desc limit :limit")

_INSERT = text(
    """
    insert into app.manual_entries (
        entry_date, kind, description, amount, currency, category_id, notes
    )
    values (
        :entry_date, :kind, :description, :amount, :currency, :category_id, :notes
    )
    returning id::text as id
    """
)

_DELETE = text("delete from app.manual_entries where id = :id")


@dataclass(frozen=True)
class ManualEntry:
    id: str
    user_id: str
    entry_date: date
    kind: str
    description: str
    amount: Decimal
    currency: str
    category_id: str | None
    category_name: str | None
    notes: str | None

    @property
    def is_income(self) -> bool:
        return self.kind == "income"


async def list_recent(conn: AsyncConnection, *, limit: int = 100) -> list[ManualEntry]:
    rows = (await conn.execute(_LIST_RECENT, {"limit": limit})).mappings()
    return [ManualEntry(**row) for row in rows]


async def create(
    conn: AsyncConnection,
    *,
    entry_date: date,
    kind: str,
    description: str,
    amount: Decimal,
    currency: str,
    category_id: str | None,
    notes: str | None,
) -> str:
    if kind not in ("income", "expense"):
        raise ValueError(f"kind debe ser 'income' o 'expense', no {kind!r}")

    row = (
        (
            await conn.execute(
                _INSERT,
                {
                    "entry_date": entry_date,
                    "kind": kind,
                    "description": description,
                    "amount": amount,
                    "currency": currency.upper(),
                    "category_id": category_id,
                    "notes": notes,
                },
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


async def delete(conn: AsyncConnection, entry_id: str) -> None:
    await conn.execute(_DELETE, {"id": entry_id})
