"""Tipos de cambio por mes: `app.fx_rates`.

# Por que la carga es manual y por mes

Un resumen argentino trae consumos en pesos y en dolares, y para ver un total
consolidado hace falta una cotizacion. Cual usar **es una decision del usuario**:
oficial, MEP, blue, o la que el banco efectivamente aplico. No hay una respuesta
correcta que el sistema pueda elegir por su cuenta, y elegir mal cambia el total
del mes en un 40%.

Por mes y no por dia porque la unidad de analisis del proyecto es el mes: un
consumo en dolares de marzo se compara con el resto de marzo, con la cotizacion
de marzo. Usar la de hoy para un consumo de hace seis meses da un numero que no
significa nada.

`user_id is null` es una cotizacion global (cargada por el seed o un admin); la
del usuario le gana, como resuelve la vista `v_monthly_base`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_SELECT = """
select
    id::text      as id,
    user_id::text as user_id,
    period_year,
    period_month,
    base_currency,
    quote_currency,
    rate,
    source,
    as_of
  from app.fx_rates
"""

_LIST = text(_SELECT + " order by period_year desc, period_month desc, base_currency")

_UPSERT = text(
    """
    insert into app.fx_rates (
        period_year, period_month, base_currency, quote_currency, rate, source, as_of
    )
    values (
        :period_year, :period_month, :base_currency, :quote_currency, :rate, 'manual', :as_of
    )
    on conflict (
        coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid),
        period_year, period_month, base_currency, quote_currency, source
    ) do update
       set rate = excluded.rate, as_of = excluded.as_of
    returning id::text as id
    """
)

_DELETE = text("delete from app.fx_rates where id = :id and user_id is not null")


@dataclass(frozen=True)
class FxRate:
    id: str
    user_id: str | None
    period_year: int
    period_month: int
    base_currency: str
    quote_currency: str
    rate: Decimal
    source: str
    as_of: date | None

    @property
    def is_global(self) -> bool:
        return self.user_id is None

    def __str__(self) -> str:
        return f"1 {self.base_currency} = {self.rate} {self.quote_currency}"


async def list_all(conn: AsyncConnection) -> list[FxRate]:
    rows = (await conn.execute(_LIST)).mappings()
    return [FxRate(**row) for row in rows]


async def upsert(
    conn: AsyncConnection,
    *,
    period_year: int,
    period_month: int,
    base_currency: str,
    quote_currency: str,
    rate: Decimal,
    as_of: date | None = None,
) -> str:
    row = (
        (
            await conn.execute(
                _UPSERT,
                {
                    "period_year": period_year,
                    "period_month": period_month,
                    "base_currency": base_currency.upper(),
                    "quote_currency": quote_currency.upper(),
                    "rate": rate,
                    "as_of": as_of,
                },
            )
        )
        .mappings()
        .one()
    )
    return str(row["id"])


async def delete(conn: AsyncConnection, rate_id: str) -> None:
    """Borra una cotizacion propia. Las globales no se tocan desde la UI."""
    await conn.execute(_DELETE, {"id": rate_id})
