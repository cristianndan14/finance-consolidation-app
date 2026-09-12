"""Lectura y escritura de `app.statements`: uno por (documento, moneda).

# Por que el upsert no pisa `status` cuando esta confirmado

Re-correr la etapa 2 sobre un resumen que el usuario ya confirmo no puede
devolverlo a `draft`: la confirmacion es una afirmacion del usuario sobre los
numeros, y un reproceso automatico no la invalida. Los totales nuevos si se
escriben — si el modelo ahora lee mejor la cabecera, mejor — pero el estado es
del usuario.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Las consultas se escriben completas en lugar de componerse con f-strings: el
# lint de bandit no puede distinguir una interpolacion constante de una con
# datos del usuario, y tener SQL armado asi en el repo invita a que la proxima
# si lleve datos.
_SELECT = """
select
    id::text          as id,
    document_id::text as document_id,
    card_id::text     as card_id,
    currency,
    period_year,
    period_month,
    closing_date,
    due_date,
    previous_balance,
    payments_credits,
    new_charges,
    total_due,
    minimum_payment,
    status,
    accepted_delta,
    notes,
    created_at,
    updated_at
  from app.statements
"""

_UPSERT = text(
    """
    insert into app.statements (
        document_id, card_id, currency, period_year, period_month,
        closing_date, due_date, previous_balance, payments_credits,
        new_charges, total_due, minimum_payment, status
    )
    values (
        :document_id, :card_id, :currency, :period_year, :period_month,
        :closing_date, :due_date, :previous_balance, :payments_credits,
        :new_charges, :total_due, :minimum_payment, :status
    )
    on conflict (user_id, document_id, currency) do update
       set card_id          = coalesce(excluded.card_id, app.statements.card_id),
           period_year      = excluded.period_year,
           period_month     = excluded.period_month,
           closing_date     = excluded.closing_date,
           due_date         = excluded.due_date,
           previous_balance = excluded.previous_balance,
           payments_credits = excluded.payments_credits,
           new_charges      = excluded.new_charges,
           total_due        = excluded.total_due,
           minimum_payment  = excluded.minimum_payment,
           -- Lo que el usuario confirmo se queda confirmado.
           status           = case
                                when app.statements.status = 'confirmed' then 'confirmed'
                                else excluded.status
                              end
    returning
    id::text          as id,
    document_id::text as document_id,
    card_id::text     as card_id,
    currency,
    period_year,
    period_month,
    closing_date,
    due_date,
    previous_balance,
    payments_credits,
    new_charges,
    total_due,
    minimum_payment,
    status,
    accepted_delta,
    notes,
    created_at,
    updated_at
    """
)

_BY_DOCUMENT = text(_SELECT + " where document_id = :document_id")
_BY_ID = text(_SELECT + " where id = :id")

_SET_STATUS = text(
    """
    update app.statements
       set status = :status,
           accepted_delta = coalesce(:accepted_delta, accepted_delta)
     where id = :id
    """
)


@dataclass(frozen=True)
class Statement:
    id: str
    document_id: str
    card_id: str | None
    currency: str
    period_year: int
    period_month: int
    closing_date: date | None
    due_date: date | None
    previous_balance: Decimal | None
    payments_credits: Decimal | None
    new_charges: Decimal | None
    total_due: Decimal | None
    minimum_payment: Decimal | None
    status: str
    accepted_delta: Decimal | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"


async def upsert(
    conn: AsyncConnection,
    *,
    document_id: str,
    currency: str,
    period_year: int,
    period_month: int,
    card_id: str | None = None,
    closing_date: date | None = None,
    due_date: date | None = None,
    previous_balance: Decimal | None = None,
    payments_credits: Decimal | None = None,
    new_charges: Decimal | None = None,
    total_due: Decimal | None = None,
    minimum_payment: Decimal | None = None,
    status: str = "draft",
) -> Statement:
    row = (
        (
            await conn.execute(
                _UPSERT,
                {
                    "document_id": document_id,
                    "card_id": card_id,
                    "currency": currency.upper(),
                    "period_year": period_year,
                    "period_month": period_month,
                    "closing_date": closing_date,
                    "due_date": due_date,
                    "previous_balance": previous_balance,
                    "payments_credits": payments_credits,
                    "new_charges": new_charges,
                    "total_due": total_due,
                    "minimum_payment": minimum_payment,
                    "status": status,
                },
            )
        )
        .mappings()
        .one()
    )
    return Statement(**row)


async def for_document(conn: AsyncConnection, document_id: str) -> list[Statement]:
    rows = (await conn.execute(_BY_DOCUMENT, {"document_id": document_id})).mappings()
    return [Statement(**row) for row in rows]


async def get(conn: AsyncConnection, statement_id: str) -> Statement | None:
    row = (await conn.execute(_BY_ID, {"id": statement_id})).mappings().one_or_none()
    return Statement(**row) if row else None


async def set_status(
    conn: AsyncConnection,
    statement_id: str,
    status: str,
    *,
    accepted_delta: Decimal | None = None,
) -> None:
    await conn.execute(
        _SET_STATUS, {"id": statement_id, "status": status, "accepted_delta": accepted_delta}
    )
