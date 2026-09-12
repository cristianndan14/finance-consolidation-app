"""Filas de transacciones confirmadas para exportar (CSV/XLSX).

Lee de `app.v_tx_enriched`, la misma vista que usa `analytics.py`: ya trae
transaccion + comercio + categoria + tarjeta, y ya aplica RLS via
`security_invoker`. El filtro de `statement_status = 'confirmed'` y
`review_status <> 'rejected'` se repite aca (la vista no lo aplica ella
misma, a diferencia de `v_monthly_cashflow`) para no exportar nunca una
transaccion sin revisar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_ROWS = text(
    """
    select posted_date,
           description_raw,
           merchant_name,
           category_name,
           signed_amount,
           currency,
           kind,
           issuer_name,
           last4
      from app.v_tx_enriched
     where period_year = :year and period_month = :month
       and statement_status = 'confirmed'
       and review_status <> 'rejected'
     order by posted_date, description_raw
    """
)


@dataclass(frozen=True)
class ExportRow:
    posted_date: date
    description: str
    merchant_name: str
    category_name: str
    amount: Decimal
    currency: str
    kind: str
    issuer_name: str | None
    last4: str | None


async def transactions_for_period(
    conn: AsyncConnection, *, year: int, month: int
) -> list[ExportRow]:
    rows = (await conn.execute(_ROWS, {"year": year, "month": month})).mappings()
    return [
        ExportRow(
            posted_date=row["posted_date"],
            description=row["description_raw"],
            merchant_name=row["merchant_name"],
            category_name=row["category_name"],
            amount=Decimal(str(row["signed_amount"])),
            currency=row["currency"],
            kind=row["kind"],
            issuer_name=row["issuer_name"],
            last4=row["last4"],
        )
        for row in rows
    ]
